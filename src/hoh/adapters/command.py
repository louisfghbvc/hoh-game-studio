"""Portable shell-free command adapter."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from hoh.adapters.base import AdapterContext
from hoh.models import CheckBundle, CheckResult, Diagnostic


class CommandAdapter:
    """Execute configured argument vectors and retain their public records."""

    def __init__(
        self,
        *,
        checks: Sequence[Sequence[str]] = (),
        required_artifacts: Sequence[str] = (),
        artifact_globs: Sequence[str] = (),
        error_patterns: Sequence[str] = (),
        entrypoints: Sequence[str] = (),
        timeout_seconds: int = 60,
    ) -> None:
        self._checks = tuple(tuple(command) for command in checks)
        self._required_artifacts = tuple(required_artifacts)
        self._artifact_globs = tuple(artifact_globs)
        self._error_patterns = tuple(re.compile(pattern) for pattern in error_patterns)
        self._entrypoints = tuple(entrypoints)
        self._timeout_seconds = timeout_seconds

    def doctor(self, project: Path) -> tuple[Diagnostic, ...]:
        """Report every configured command executable unavailable to the product."""

        diagnostics: list[Diagnostic] = []
        for command in self._checks:
            executable = command[0] if command else ""
            if not _executable_exists(project, executable):
                diagnostics.append(
                    Diagnostic(
                        "command:executable",
                        "blocked",
                        f"Command executable was not found: {executable or '<empty>'}",
                    )
                )
        return tuple(diagnostics)

    def summarize(self, project: Path) -> dict[str, object]:
        """Return a content-free summary of the current tracked project tree."""

        tracked_paths = tuple(
            path for path in _git_output(project, "ls-files", "-z").split("\0") if path
        )
        suffix_counts: dict[str, int] = {}
        for path in tracked_paths:
            suffix = Path(path).suffix.lower() or "<none>"
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
        return {
            "tracked_file_count": len(tracked_paths),
            "language_suffix_counts": dict(sorted(suffix_counts.items())),
            "sha": _git_output(project, "rev-parse", "HEAD").strip(),
            "entrypoints": self._entrypoints,
        }

    def baseline(
        self, context: AdapterContext, plan: Mapping[str, object]
    ) -> CheckBundle:
        """Run the same strict checks before a developer changes the product."""

        return self.check(context, plan)

    def check(
        self, context: AdapterContext, plan: Mapping[str, object]
    ) -> CheckBundle:
        del plan
        records_path = context.output / "checks"
        records_path.mkdir(parents=True, exist_ok=True)
        results: list[CheckResult] = []
        retained_paths: list[str] = []
        for index, command in enumerate(self._checks, start=1):
            result, paths = self._run_command(context.project, records_path, index, command)
            results.append(result)
            retained_paths.extend(paths)
        for artifact in self._required_artifacts:
            artifact_path = context.project / artifact
            if artifact_path.is_file():
                results.append(
                    CheckResult(
                        f"artifact:{artifact}",
                        "pass",
                        f"Required artifact exists: {artifact}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        f"artifact:{artifact}",
                        "fail",
                        f"Required artifact is missing: {artifact}",
                    )
                )
        status = _bundle_status(results)
        return CheckBundle("command", status, tuple(results), tuple(retained_paths))

    def _run_command(
        self, project: Path, records_path: Path, index: int, command: tuple[str, ...]
    ) -> tuple[CheckResult, tuple[str, ...]]:
        stem = f"command-{index:04d}"
        stdout_path = records_path / f"{stem}.stdout.txt"
        stderr_path = records_path / f"{stem}.stderr.txt"
        metadata_path = records_path / f"{stem}.json"
        retained_paths = tuple(
            str(path.relative_to(records_path.parent.parent)).replace("\\", "/")
            for path in (stdout_path, stderr_path, metadata_path)
        )
        started_at = _utc_now()
        timed_out = False
        return_code: int | None = None
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            return_code = completed.returncode
            status = "pass" if completed.returncode == 0 else "fail"
            summary = (
                "Command completed successfully"
                if status == "pass"
                else f"Command exited with status {completed.returncode}"
            )
            pattern = _matching_pattern(self._error_patterns, stdout, stderr)
            if pattern is not None:
                status = "fail"
                summary = f"Command output matched error pattern: {pattern.pattern}"
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = _coerce_output(error.stdout)
            stderr = _coerce_output(error.stderr)
            status = "fail"
            summary = f"Command timed out after {self._timeout_seconds} seconds"
        except (FileNotFoundError, OSError):
            stdout = ""
            stderr = ""
            status = "blocked"
            summary = f"Command executable was not found: {command[0] if command else '<empty>'}"
        ended_at = _utc_now()
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "command": list(command),
                    "return_code": return_code,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "timed_out": timed_out,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return (
            CheckResult(f"command:{index:04d}", status, summary, retained_paths),
            retained_paths,
        )

    def collect(self, context: AdapterContext, bundle: CheckBundle) -> dict[str, str]:
        """Copy configured candidate artifacts into host-owned output with hashes."""

        del bundle
        project = context.project.resolve()
        destination_root = context.output / "artifacts"
        artifacts: dict[str, str] = {}
        sources: dict[str, Path] = {}
        for pattern in self._artifact_globs:
            for source in context.project.glob(pattern):
                resolved = source.resolve()
                if source.is_file() and resolved.is_relative_to(project):
                    relative = resolved.relative_to(project).as_posix()
                    sources[relative] = resolved
        for relative, source in sorted(sources.items()):
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            artifacts[str(destination.relative_to(context.output)).replace("\\", "/")] = _sha256(
                destination
            )
        return artifacts


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _bundle_status(results: Sequence[CheckResult]) -> str:
    if any(result.status == "blocked" for result in results):
        return "blocked"
    if any(result.status == "fail" for result in results):
        return "fail"
    return "pass"


def _matching_pattern(
    patterns: Sequence[re.Pattern[str]], stdout: str, stderr: str
) -> re.Pattern[str] | None:
    combined = stdout + stderr
    return next((pattern for pattern in patterns if pattern.search(combined)), None)


def _executable_exists(project: Path, executable: str) -> bool:
    if not executable:
        return False
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate if candidate.is_absolute() else project / candidate
        return resolved.is_file()
    return shutil.which(executable) is not None


def _git_output(project: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    ).stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
