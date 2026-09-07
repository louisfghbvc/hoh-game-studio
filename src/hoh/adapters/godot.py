"""Strict, deterministic checks for a Godot project candidate."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from hoh.adapters.base import AdapterContext
from hoh.adapters.command import CommandAdapter, _validated_roots
from hoh.models import CheckBundle, CheckResult, Diagnostic

_RUNTIME_ERROR_PATTERNS = (r"SCRIPT ERROR", r"Parse Error", r"ERROR:")


class GodotAdapter:
    """Check one contained Godot project with an explicit, shell-free command prefix."""

    def __init__(
        self,
        *,
        command: Sequence[str] = ("godot",),
        project_subdir: str = ".",
        test_commands: Sequence[Sequence[str]] = (),
        replay_commands: Sequence[Sequence[str]] = (),
        required_evidence_globs: Sequence[str] = (),
        timeout_seconds: int = 60,
    ) -> None:
        self._command = tuple(command)
        self._project_subdir = project_subdir
        self._test_commands = tuple(tuple(arguments) for arguments in test_commands)
        self._replay_commands = tuple(tuple(arguments) for arguments in replay_commands)
        self._required_evidence_globs = tuple(required_evidence_globs)
        self._timeout_seconds = timeout_seconds

    def doctor(self, project: Path) -> tuple[Diagnostic, ...]:
        """Block absent Godot before reporting product-local project diagnostics."""
        if not self._executable_available(project):
            return (
                Diagnostic("godot:executable", "blocked", "Godot executable was not found"),
            )
        if self._project_root(project.resolve()) is None:
            return (
                Diagnostic("godot:project", "blocked", "Godot project.godot was not found"),
            )
        return ()

    def summarize(self, project: Path) -> dict[str, object]:
        """Return the generic tracked-file summary with the Godot config entrypoint."""
        entrypoint = str(Path(self._project_subdir) / "project.godot")
        return CommandAdapter(entrypoints=(entrypoint,)).summarize(project)

    def baseline(
        self, context: AdapterContext, plan: Mapping[str, object]
    ) -> CheckBundle:
        """Use the candidate checks as the deterministic baseline."""
        return self.check(context, plan)

    def check(
        self, context: AdapterContext, plan: Mapping[str, object]
    ) -> CheckBundle:
        """Run deterministic Godot boot, configured commands, and evidence checks."""
        del plan
        project, _ = _validated_roots(context)
        if not self._executable_available(project):
            return CheckBundle(
                "godot",
                "blocked",
                (
                    CheckResult(
                        "godot:executable", "blocked", "Godot executable was not found"
                    ),
                ),
            )

        results = [CheckResult("godot:executable", "pass", "Godot executable is available")]
        godot_project = self._project_root(project)
        if godot_project is None:
            results.append(
                CheckResult("godot:project", "fail", "Godot project.godot was not found")
            )
            return CheckBundle("godot", "fail", tuple(results))
        results.append(CheckResult("godot:project", "pass", "Godot project.godot exists"))

        command_ids = ["godot:headless-import"]
        commands = [
            self._command
            + ("--headless", "--path", str(godot_project), "--editor", "--quit")
        ]
        for index, arguments in enumerate(self._test_commands, start=1):
            command_ids.append(f"godot:test:{index:04d}")
            commands.append(self._command + self._expand_project(arguments, godot_project))
        for index, arguments in enumerate(self._replay_commands, start=1):
            command_ids.append(f"godot:replay:{index:04d}")
            commands.append(self._command + self._expand_project(arguments, godot_project))

        runner = CommandAdapter(
            checks=commands,
            artifact_globs=self._candidate_artifact_globs(project, godot_project),
            error_patterns=_RUNTIME_ERROR_PATTERNS,
            timeout_seconds=self._timeout_seconds,
        )
        command_bundle = runner.check(context, {})
        command_results = tuple(
            CheckResult(
                command_id,
                result.status,
                result.summary,
                result.artifact_paths,
            )
            for command_id, result in zip(command_ids, command_bundle.results, strict=True)
        )
        results.extend(command_results)
        runtime_error = any(
            result.summary.startswith("Command output matched error pattern:")
            for result in command_results
        )
        runtime_artifacts = tuple(
            path for result in command_results for path in result.artifact_paths
        )
        results.append(
            CheckResult(
                "godot:runtime-errors",
                "fail" if runtime_error else "pass",
                "Godot output contained a runtime error"
                if runtime_error
                else "Godot output contained no runtime error patterns",
                runtime_artifacts,
            )
        )
        results.extend(self._evidence_results(godot_project))
        status = _bundle_status(results)
        artifacts = tuple(
            dict.fromkeys(path for result in results for path in result.artifact_paths)
        )
        return CheckBundle("godot", status, tuple(results), artifacts)

    def collect(self, context: AdapterContext, bundle: CheckBundle) -> dict[str, str]:
        """Collect only files matching configured, contained Godot evidence globs."""
        del bundle
        project, _ = _validated_roots(context)
        godot_project = self._project_root(project)
        if godot_project is None:
            return {}
        runner = CommandAdapter(
            artifact_globs=self._candidate_artifact_globs(project, godot_project)
        )
        return runner.collect(context, CheckBundle("godot", "pass", ()))

    def _executable_available(self, project: Path) -> bool:
        return not CommandAdapter(checks=(self._command,)).doctor(project)

    def _project_root(self, candidate: Path) -> Path | None:
        root = (candidate / self._project_subdir).resolve()
        if not root.is_relative_to(candidate) or not (root / "project.godot").is_file():
            return None
        return root

    def _expand_project(self, arguments: tuple[str, ...], project: Path) -> tuple[str, ...]:
        return tuple(str(project) if argument == "{project}" else argument for argument in arguments)

    def _evidence_results(self, project: Path) -> tuple[CheckResult, ...]:
        results: list[CheckResult] = []
        for pattern in self._required_evidence_globs:
            matches = _contained_glob(project, pattern)
            results.append(
                CheckResult(
                    f"godot:evidence:{pattern}",
                    "pass" if matches else "fail",
                    f"Required Godot evidence matched: {pattern}"
                    if matches
                    else f"Required Godot evidence is missing: {pattern}",
                )
            )
        return tuple(results)

    def _candidate_artifact_globs(self, candidate: Path, project: Path) -> tuple[str, ...]:
        prefix = project.relative_to(candidate)
        return tuple(
            str(prefix / pattern)
            for pattern in self._required_evidence_globs
            if _is_safe_glob(pattern)
        )


def _contained_glob(project: Path, pattern: str) -> tuple[Path, ...]:
    if not _is_safe_glob(pattern):
        return ()
    try:
        return tuple(
            path
            for path in project.glob(pattern)
            if path.is_file() and path.resolve().is_relative_to(project)
        )
    except (NotImplementedError, OSError, ValueError):
        return ()


def _is_safe_glob(pattern: str) -> bool:
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        return False
    path = Path(pattern)
    return not path.drive and not path.root and ".." not in path.parts


def _bundle_status(results: Sequence[CheckResult]) -> str:
    if any(result.status == "blocked" for result in results):
        return "blocked"
    if any(result.status == "fail" for result in results):
        return "fail"
    return "pass"
