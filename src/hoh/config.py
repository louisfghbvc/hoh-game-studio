from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from hoh.models import Diagnostic, HarnessConfig


class ConfigError(ValueError):
    """Raised when HoH project configuration is missing or invalid."""


KNOWN_ADAPTERS = frozenset({"command", "godot"})
DEFAULTS: dict[str, object] = {
    "codex_bin": "codex",
    "max_loops": 12,
    "max_priorities_per_loop": 3,
    "max_role_retries": 1,
    "max_consecutive_no_progress": 3,
    "max_consecutive_same_blocker": 3,
    "role_timeout_minutes": 45,
    "max_total_tokens": 5_000_000,
    "max_elapsed_minutes": 480,
    "protected_paths": (".hoh", ".git"),
}
_REQUIRED_KEYS = frozenset({"adapter", "model", "reasoning_effort", *DEFAULTS})
_ALLOWED_KEYS = _REQUIRED_KEYS | {"adapter_options"}


def _require_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} is required")
    return value


def _require_positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _validate_inputs(adapter: object, model: object, reasoning_effort: object) -> tuple[str, str, str]:
    if not isinstance(adapter, str) or adapter not in KNOWN_ADAPTERS:
        raise ConfigError(f"unknown adapter: {adapter}")
    return (
        adapter,
        _require_nonempty_string(model, "model"),
        _require_nonempty_string(reasoning_effort, "reasoning_effort"),
    )


def _project_path(project: Path) -> Path:
    path = Path(project).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"project directory does not exist: {path}")
    return path


def _require_git_repository(project: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ConfigError("Git repository is required") from error
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise ConfigError("Git repository is required")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_config(adapter: str, model: str, reasoning_effort: str) -> str:
    lines = [
        f"adapter = {_toml_string(adapter)}",
        f"model = {_toml_string(model)}",
        f"reasoning_effort = {_toml_string(reasoning_effort)}",
    ]
    for key, value in DEFAULTS.items():
        if isinstance(value, str):
            rendered = _toml_string(value)
        elif isinstance(value, tuple):
            rendered = "[" + ", ".join(_toml_string(item) for item in value) + "]"
        else:
            rendered = str(value)
        lines.append(f"{key} = {rendered}")
    lines.extend(["", "[adapter_options]", ""])
    return "\n".join(lines)


def _write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def initialize_project(
    project: Path, adapter: str, model: str, reasoning_effort: str
) -> Path:
    """Create the durable HoH state scaffold for a Git product repository."""

    adapter, model, reasoning_effort = _validate_inputs(adapter, model, reasoning_effort)
    project_path = _project_path(project)
    _require_git_repository(project_path)

    state_path = project_path / ".hoh"
    state_path.mkdir(exist_ok=True)
    config_path = state_path / "config.toml"
    _write_if_absent(config_path, _render_config(adapter, model, reasoning_effort))
    _write_if_absent(state_path / "prd.md", "# Product requirements\n")
    _write_if_absent(
        state_path / "requirements.json",
        json.dumps({"schema_version": 1, "claims": []}, indent=2) + "\n",
    )
    _write_if_absent(
        state_path / "issue-ledger.json",
        json.dumps({"schema_version": 1, "issues": []}, indent=2) + "\n",
    )
    (state_path / "runs").mkdir(exist_ok=True)
    return config_path


def _read_config(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not read configuration: {path}") from error
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a TOML table")
    return data


def _protected_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("protected_paths must be a nonempty list of strings")
    if any(not isinstance(path, str) or not path for path in value):
        raise ConfigError("protected_paths must be a nonempty list of strings")
    return tuple(value)


def load_config(project: Path) -> HarnessConfig:
    """Load and strictly validate a product's explicit HoH configuration."""

    project_path = _project_path(project)
    config_path = project_path / ".hoh" / "config.toml"
    if not config_path.is_file():
        raise ConfigError(f"configuration does not exist: {config_path}")
    data = _read_config(config_path)
    unknown_keys = set(data) - _ALLOWED_KEYS
    if unknown_keys:
        raise ConfigError(f"unknown configuration fields: {', '.join(sorted(unknown_keys))}")
    missing_keys = _REQUIRED_KEYS - set(data)
    if missing_keys:
        raise ConfigError(f"missing configuration fields: {', '.join(sorted(missing_keys))}")

    adapter, model, reasoning_effort = _validate_inputs(
        data["adapter"], data["model"], data["reasoning_effort"]
    )
    adapter_options = data.get("adapter_options", {})
    if not isinstance(adapter_options, Mapping):
        raise ConfigError("adapter_options must be a TOML table")

    return HarnessConfig(
        project=project_path,
        adapter=adapter,
        model=model,
        reasoning_effort=reasoning_effort,
        codex_bin=_require_nonempty_string(data["codex_bin"], "codex_bin"),
        max_loops=_require_positive_integer(data["max_loops"], "max_loops"),
        max_priorities_per_loop=_require_positive_integer(
            data["max_priorities_per_loop"], "max_priorities_per_loop"
        ),
        max_role_retries=_require_positive_integer(data["max_role_retries"], "max_role_retries"),
        max_consecutive_no_progress=_require_positive_integer(
            data["max_consecutive_no_progress"], "max_consecutive_no_progress"
        ),
        max_consecutive_same_blocker=_require_positive_integer(
            data["max_consecutive_same_blocker"], "max_consecutive_same_blocker"
        ),
        role_timeout_minutes=_require_positive_integer(
            data["role_timeout_minutes"], "role_timeout_minutes"
        ),
        max_total_tokens=_require_positive_integer(
            data["max_total_tokens"], "max_total_tokens"
        ),
        max_elapsed_minutes=_require_positive_integer(
            data["max_elapsed_minutes"], "max_elapsed_minutes"
        ),
        protected_paths=_protected_paths(data["protected_paths"]),
        adapter_options=MappingProxyType(dict(adapter_options)),
    )


def doctor(config: HarnessConfig) -> tuple[Diagnostic, ...]:
    """Return configuration-level blockers before a run can begin."""

    requirements_path = config.project / ".hoh" / "requirements.json"
    try:
        requirements: Any = json.loads(requirements_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        requirements = None
    claims = requirements.get("claims") if isinstance(requirements, dict) else None
    duplicate_claim_ids = _duplicate_claim_ids(claims)
    if duplicate_claim_ids:
        return (
            Diagnostic(
                "requirements:duplicate-claim-id",
                "blocked",
                "requirements.json must not contain duplicate claim IDs: "
                + ", ".join(duplicate_claim_ids),
            ),
        )
    has_required_claim = isinstance(claims, list) and any(
        isinstance(claim, dict) and claim.get("required") is True for claim in claims
    )
    if not has_required_claim:
        return (
            Diagnostic(
                "requirements:required-claim",
                "blocked",
                "requirements.json must contain at least one required claim",
            ),
        )
    return ()


def _duplicate_claim_ids(claims: object) -> tuple[str, ...]:
    if not isinstance(claims, list):
        return ()
    seen: set[str] = set()
    duplicates: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if claim_id in seen:
            duplicates.add(claim_id)
        seen.add(claim_id)
    return tuple(sorted(duplicates))


def handle_init(arguments: argparse.Namespace) -> int:
    try:
        config_path = initialize_project(
            Path(arguments.project),
            arguments.adapter,
            arguments.model,
            arguments.reasoning_effort,
        )
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    print(f"Initialized HoH project at {config_path.parent}")
    return 0


def handle_doctor(arguments: argparse.Namespace) -> int:
    try:
        diagnostics = doctor(load_config(Path(arguments.project)))
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    for diagnostic in diagnostics:
        print(f"{diagnostic.severity}: {diagnostic.code}: {diagnostic.message}")
    return 3 if any(item.severity == "blocked" for item in diagnostics) else 0
