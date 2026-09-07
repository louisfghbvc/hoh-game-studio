from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from hoh.adapters.base import ProductAdapter
from hoh.adapters.command import CommandAdapter
from hoh.adapters.godot import GodotAdapter
from hoh.backends.base import (
    AgentBackend,
    BackendProcessError,
    BackendProtocolError,
    BackendTimeout,
)
from hoh.backends.codex_exec import CodexExecBackend
from hoh.config import (
    ConfigError,
    doctor as config_doctor,
    handle_init,
    load_config,
    validated_adapter_options,
)
from hoh.models import Diagnostic, HarnessConfig, Role
from hoh.orchestrator import (
    HoHOrchestrator,
    OrchestratorError,
    PreflightError,
    ResumeError,
    RoleOutputError,
)
from hoh.policy import StopPolicy
from hoh.prompts import PromptRenderer, PromptRenderingError
from hoh.reporting import render_run_summary, write_run_summary
from hoh.skills.registry import SkillRegistry, SkillValidationError
from hoh.state.evidence import EvidenceBindingError
from hoh.state.issue_ledger import IssueLedgerError
from hoh.state.store import RunLockedError, StateConflictError, StateError, StateStore
from hoh.vcs.git import DirtyWorktreeError, GitError, GitService, ProtectedPathError


_SUCCESS_STATUSES = frozenset({"complete"})
_INCOMPLETE_STATUSES = frozenset({"blocked", "budget_exhausted"})
_CANCELLED_STATUSES = frozenset({"cancelled"})
_PROTOCOL_ERRORS = (
    BackendProtocolError,
    EvidenceBindingError,
    IssueLedgerError,
    ProtectedPathError,
    RoleOutputError,
    StateConflictError,
)
_BLOCKED_ERRORS = (
    BackendProcessError,
    BackendTimeout,
    DirtyWorktreeError,
    GitError,
    OSError,
    OrchestratorError,
    PreflightError,
    PromptRenderingError,
    RunLockedError,
    StateError,
)


@dataclass(frozen=True)
class Services:
    """Fully composed host services for one configured product."""

    orchestrator: HoHOrchestrator
    backend: AgentBackend
    adapter: ProductAdapter
    git: GitService
    store: StateStore
    skills: SkillRegistry
    policy: StopPolicy
    prompt_renderer: PromptRenderer
    schema_dir: Path


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hoh")
    commands = parser.add_subparsers(dest="command")

    init_parser = commands.add_parser("init", help="initialize HoH state in a product")
    init_parser.add_argument("--project", default=".")
    init_parser.add_argument("--adapter", required=True, choices=("command", "godot"))
    init_parser.add_argument("--model", required=True)
    init_parser.add_argument("--reasoning-effort", required=True)
    init_parser.set_defaults(handler=_handle_init)

    doctor_parser = commands.add_parser("doctor", help="check run prerequisites")
    doctor_parser.add_argument("--project", default=".")
    doctor_parser.set_defaults(handler=_handle_doctor)

    run_parser = commands.add_parser("run", help="start a bounded HoH run")
    run_parser.add_argument("--project", default=".")
    run_parser.add_argument("--max-loops", type=_positive_integer)
    run_parser.set_defaults(handler=_handle_run)

    status_parser = commands.add_parser("status", help="show the newest durable run")
    status_parser.add_argument("--project", default=".")
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    status_parser.set_defaults(handler=_handle_status)

    resume_parser = commands.add_parser("resume", help="continue the newest resumable run")
    resume_parser.add_argument("--project", default=".")
    resume_parser.add_argument("--run-id")
    resume_parser.set_defaults(handler=_handle_resume)

    report_parser = commands.add_parser("report", help="render the newest run report")
    report_parser.add_argument("--project", default=".")
    report_parser.set_defaults(handler=_handle_report)

    skills_parser = commands.add_parser("skills", help="inspect packaged skills")
    skill_commands = skills_parser.add_subparsers(dest="skills_command")
    list_parser = skill_commands.add_parser("list", help="list packaged skill versions")
    list_parser.set_defaults(handler=_handle_skills_list)
    return parser


def build_services(
    config: HarnessConfig,
    backend_name: str | AgentBackend = "codex-exec",
) -> Services:
    """Compose existing host services without exposing a fake CLI option.

    Tests may pass an ``AgentBackend`` object directly. String selection is
    deliberately restricted to the production ``codex-exec`` backend.
    """

    package_root = resources.files("hoh")
    prompt_root = package_root.joinpath("resources", "prompts")
    schema_dir = _filesystem_resource_directory(
        package_root.joinpath("resources", "schemas"), "schema"
    )
    skill_dir = _filesystem_resource_directory(
        package_root.joinpath("resources", "skills"), "skill"
    )
    templates = {
        role: prompt_root.joinpath(f"{role.value}.md").read_text(encoding="utf-8")
        for role in Role
    }
    prompt_renderer = PromptRenderer(templates)
    skills = SkillRegistry.load(skill_dir)

    if isinstance(backend_name, str):
        if backend_name != "codex-exec":
            raise ConfigError(f"unknown backend: {backend_name}")
        backend: AgentBackend = CodexExecBackend(config.codex_bin)
    else:
        run = getattr(backend_name, "run", None)
        if not callable(run):
            raise ConfigError("injected backend must implement run(request)")
        backend = backend_name

    options = validated_adapter_options(config)
    adapter: ProductAdapter
    if config.adapter == "command":
        adapter = CommandAdapter(**options)  # type: ignore[arg-type]
    elif config.adapter == "godot":
        adapter = GodotAdapter(**options)  # type: ignore[arg-type]
    else:  # HarnessConfig normally comes from strict load_config.
        raise ConfigError(f"unknown adapter: {config.adapter}")

    git = GitService(config.project)
    store = StateStore(config.project)
    policy = StopPolicy(config)
    orchestrator = HoHOrchestrator(
        config,
        backend,
        adapter,
        git,
        store,
        skills,
        policy,
        prompt_renderer=prompt_renderer,
        schema_dir=schema_dir,
    )
    return Services(
        orchestrator,
        backend,
        adapter,
        git,
        store,
        skills,
        policy,
        prompt_renderer,
        schema_dir,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: AgentBackend | None = None,
) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    handler = getattr(arguments, "handler", None)
    if handler is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        return int(handler(arguments, backend))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("run cancelled by user", file=sys.stderr)
        return 130
    except (ConfigError, ResumeError, SkillValidationError) as error:
        print(_error_message(error), file=sys.stderr)
        return 2
    except (PreflightError, RunLockedError) as error:
        print(_error_message(error), file=sys.stderr)
        return 3
    except _PROTOCOL_ERRORS as error:
        print(_error_message(error), file=sys.stderr)
        return 5
    except _BLOCKED_ERRORS as error:
        print(_error_message(error), file=sys.stderr)
        return 3
    except Exception as error:
        # Task 13 classifies unexpected adapter/process failures as infrastructure.
        print(_error_message(error), file=sys.stderr)
        return 3


def _handle_init(arguments: argparse.Namespace, backend: AgentBackend | None) -> int:
    del backend
    return handle_init(arguments)


def _handle_doctor(arguments: argparse.Namespace, backend: AgentBackend | None) -> int:
    config = load_config(Path(arguments.project))
    services = build_services(
        config, backend_name=backend if backend is not None else "codex-exec"
    )
    diagnostics = _prerequisite_diagnostics(config, services)
    if not diagnostics:
        print("info: doctor: all prerequisites are available")
        return 0
    for diagnostic in diagnostics:
        print(f"{diagnostic.severity}: {diagnostic.code}: {diagnostic.message}")
    return 3 if any(item.severity == "blocked" for item in diagnostics) else 0


def _handle_run(arguments: argparse.Namespace, backend: AgentBackend | None) -> int:
    config = load_config(Path(arguments.project))
    _assert_no_existing_lock(config.project)
    services = build_services(
        config, backend_name=backend if backend is not None else "codex-exec"
    )
    diagnostics = _prerequisite_diagnostics(config, services)
    if any(item.severity == "blocked" for item in diagnostics):
        for diagnostic in diagnostics:
            print(f"{diagnostic.severity}: {diagnostic.code}: {diagnostic.message}")
        return 3
    result = _normalized_result(services.orchestrator.run(arguments.max_loops))
    exit_code = _terminal_exit(result)
    _print_human_status(result)
    return exit_code


def _handle_status(arguments: argparse.Namespace, backend: AgentBackend | None) -> int:
    config = load_config(Path(arguments.project))
    _, status = _latest_status(config, backend)
    if arguments.as_json:
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    else:
        _print_human_status(status)
    return 0


def _handle_resume(arguments: argparse.Namespace, backend: AgentBackend | None) -> int:
    config = load_config(Path(arguments.project))
    _assert_no_existing_lock(config.project)
    services = build_services(
        config, backend_name=backend if backend is not None else "codex-exec"
    )
    result = _normalized_result(
        services.orchestrator.resume(expected_run_id=arguments.run_id)
    )
    exit_code = _terminal_exit(result)
    _print_human_status(result)
    return exit_code


def _handle_report(arguments: argparse.Namespace, backend: AgentBackend | None) -> int:
    config = load_config(Path(arguments.project))
    run_dir, status = _latest_status(config, backend)
    summary = render_run_summary(status)
    write_run_summary(run_dir, summary)
    print(summary, end="")
    return 0


def _handle_skills_list(
    arguments: argparse.Namespace, backend: AgentBackend | None
) -> int:
    del arguments, backend
    package_root = resources.files("hoh")
    skill_dir = _filesystem_resource_directory(
        package_root.joinpath("resources", "skills"), "skill"
    )
    registry = SkillRegistry.load(skill_dir)
    documents = sorted(registry._documents.values(), key=lambda item: item.skill_id)
    for document in documents:
        roles = ",".join(role.value for role in document.roles)
        adapters = ",".join(document.adapters)
        print(
            f"{document.skill_id} {document.version} "
            f"roles={roles} adapters={adapters} sha256={document.sha256}"
        )
    return 0


def _latest_status(
    config: HarnessConfig, backend: AgentBackend | None = None
) -> tuple[Path, dict[str, object]]:
    services = build_services(
        config, backend_name=backend if backend is not None else "codex-exec"
    )
    run_dir, status = services.orchestrator.inspect_latest()
    return run_dir, _normalized_result(status)


def _normalized_result(result: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(result, Mapping):
        raise StateConflictError("orchestrator result must be a structured object")
    document = dict(result)
    terminal_status = document.get("terminal_status")
    status_alias = document.get("status")
    if terminal_status is not None and not isinstance(terminal_status, str):
        raise StateConflictError("orchestrator result has an invalid terminal status")
    if status_alias is not None and not isinstance(status_alias, str):
        raise StateConflictError("orchestrator result has an invalid status alias")
    if (
        terminal_status is not None
        and status_alias is not None
        and terminal_status != status_alias
    ):
        raise StateConflictError("orchestrator result has conflicting structured status aliases")
    status = terminal_status if terminal_status is not None else status_alias
    if not isinstance(status, str):
        raise StateConflictError("orchestrator result has no structured status")
    document["terminal_status"] = status
    document["status"] = status
    if "completed_loops" in document:
        document.setdefault("loops_completed", document["completed_loops"])
    elif "loops_completed" in document:
        document.setdefault("completed_loops", document["loops_completed"])
    return document


def _terminal_exit(result: Mapping[str, object]) -> int:
    status = result.get("status")
    failure_category = result.get("failure_category")
    if failure_category is not None and (
        failure_category not in {"infrastructure", "protocol"} or status != "blocked"
    ):
        raise StateConflictError("orchestrator result has an invalid failure category")
    if failure_category == "infrastructure":
        return 3
    if failure_category == "protocol":
        return 5
    if status in _SUCCESS_STATUSES:
        return 0
    if status in _INCOMPLETE_STATUSES:
        return 4
    if status in _CANCELLED_STATUSES:
        return 130
    raise StateConflictError(f"unexpected orchestrator terminal status: {status}")


def _print_human_status(status: Mapping[str, object]) -> None:
    print(render_run_summary(status), end="")


def _assert_no_existing_lock(project: Path) -> None:
    lock_path = project / ".hoh" / "lock"
    if lock_path.exists():
        raise RunLockedError(f"run lock already exists at {lock_path}")


def _host_diagnostics(config: HarnessConfig, services: Services) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    lock_path = config.project / ".hoh" / "lock"
    if lock_path.exists():
        diagnostics.append(
            Diagnostic("state:lock", "blocked", f"Run lock already exists at {lock_path}")
        )

    if isinstance(services.backend, CodexExecBackend) and not _executable_exists(
        config.project, config.codex_bin
    ):
        diagnostics.append(
            Diagnostic("codex:executable", "blocked", "Codex executable was not found")
        )

    state_root = config.project / ".hoh"
    probe: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=state_root) as stream:
            probe = Path(stream.name)
    except OSError:
        diagnostics.append(
            Diagnostic("state:writable", "blocked", "HoH state directory is not writable")
        )
    finally:
        if probe is not None:
            probe.unlink(missing_ok=True)

    try:
        if shutil.disk_usage(config.project).free <= 0:
            diagnostics.append(
                Diagnostic("disk:space", "blocked", "No free disk space is available")
            )
    except OSError:
        diagnostics.append(
            Diagnostic("disk:space", "blocked", "Available disk space could not be determined")
        )

    try:
        services.git.head_sha()
        if not lock_path.exists():
            services.git.assert_clean()
    except DirtyWorktreeError as error:
        diagnostics.append(Diagnostic("git:clean", "blocked", str(error)))
    except GitError as error:
        diagnostics.append(Diagnostic("git:repository", "blocked", str(error)))
    return tuple(diagnostics)


def _executable_exists(project: Path, executable: str) -> bool:
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate if candidate.is_absolute() else project / candidate
        return resolved.is_file()
    return shutil.which(executable) is not None


def _deduplicated_diagnostics(
    diagnostics: Sequence[Diagnostic],
) -> tuple[Diagnostic, ...]:
    return tuple(dict.fromkeys(diagnostics))


def _prerequisite_diagnostics(
    config: HarnessConfig, services: Services
) -> tuple[Diagnostic, ...]:
    """Collect the exact prerequisite set shared by ``doctor`` and ``run``."""

    return _deduplicated_diagnostics(
        (
            *config_doctor(config),
            *_host_diagnostics(config, services),
            *services.adapter.doctor(config.project),
        )
    )


def _filesystem_resource_directory(resource: object, label: str) -> Path:
    try:
        path = Path(resource)  # type: ignore[arg-type]
    except TypeError:
        path = Path(str(resource))
    if not path.is_dir():
        raise ConfigError(f"packaged {label} resource directory does not exist: {path}")
    return path.resolve()


def _error_message(error: BaseException) -> str:
    return str(error) or type(error).__name__


def entrypoint() -> None:
    raise SystemExit(main())
