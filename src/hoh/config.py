from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

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
_COMMAND_ADAPTER_KEYS = frozenset(
    {
        "checks",
        "required_artifacts",
        "artifact_globs",
        "error_patterns",
        "entrypoints",
        "timeout_seconds",
    }
)
_GODOT_ADAPTER_KEYS = frozenset(
    {
        "command",
        "project_subdir",
        "test_commands",
        "replay_commands",
        "required_evidence_globs",
        "timeout_seconds",
    }
)


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


def validated_adapter_options(config: HarnessConfig) -> dict[str, object]:
    """Return constructor-ready adapter options after strict shape validation.

    ``HarnessConfig`` retains arbitrary TOML tables so loading remains a pure
    configuration operation.  The concrete adapter boundary validates the
    selected table immediately before dependency construction.
    """

    options = dict(config.adapter_options)
    allowed = (
        _COMMAND_ADAPTER_KEYS if config.adapter == "command" else _GODOT_ADAPTER_KEYS
    )
    unknown = set(options) - allowed
    if unknown:
        raise ConfigError(
            "unknown adapter_options fields for "
            f"{config.adapter}: {', '.join(sorted(str(item) for item in unknown))}"
        )

    if config.adapter == "command":
        result: dict[str, object] = {
            "checks": _command_vectors(options, "checks"),
            "required_artifacts": _string_sequence(options, "required_artifacts"),
            "artifact_globs": _string_sequence(options, "artifact_globs"),
            "error_patterns": _string_sequence(options, "error_patterns"),
            "entrypoints": _string_sequence(options, "entrypoints"),
            "timeout_seconds": _option_positive_integer(options, "timeout_seconds", 60),
        }
        for pattern in result["error_patterns"]:  # type: ignore[union-attr]
            try:
                re.compile(pattern)
            except re.error as error:
                raise ConfigError(
                    f"adapter_options.error_patterns contains an invalid regular expression: {pattern}"
                ) from error
        return result

    command = _string_sequence(options, "command", default=("godot",))
    if not command:
        raise ConfigError("adapter_options.command must be a nonempty list of strings")
    project_subdir = options.get("project_subdir", ".")
    if not isinstance(project_subdir, str) or not project_subdir:
        raise ConfigError("adapter_options.project_subdir must be a nonempty string")
    return {
        "command": command,
        "project_subdir": project_subdir,
        "test_commands": _command_vectors(options, "test_commands"),
        "replay_commands": _command_vectors(options, "replay_commands"),
        "required_evidence_globs": _string_sequence(
            options, "required_evidence_globs"
        ),
        "timeout_seconds": _option_positive_integer(options, "timeout_seconds", 60),
    }


def _string_sequence(
    options: Mapping[str, object],
    name: str,
    *,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = options.get(name, default)
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigError(f"adapter_options.{name} must be a list of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"adapter_options.{name} must be a list of strings")
    return tuple(value)


def _command_vectors(
    options: Mapping[str, object], name: str
) -> tuple[tuple[str, ...], ...]:
    value = options.get(name, ())
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigError(f"adapter_options.{name} must be a list of argument lists")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if (
            not isinstance(command, (list, tuple))
            or isinstance(command, (str, bytes, bytearray))
            or not command
            or any(not isinstance(argument, str) or not argument for argument in command)
        ):
            raise ConfigError(
                f"adapter_options.{name} must be a list of nonempty argument lists"
            )
        commands.append(tuple(command))
    return tuple(commands)


def _option_positive_integer(
    options: Mapping[str, object], name: str, default: int
) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"adapter_options.{name} must be a positive integer")
    return value


def doctor(config: HarnessConfig) -> tuple[Diagnostic, ...]:
    """Return configuration-level blockers before a run can begin."""

    requirements_path = config.project / ".hoh" / "requirements.json"
    try:
        requirements: Any = json.loads(requirements_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return (
            Diagnostic(
                "requirements:schema",
                "blocked",
                f"requirements.json could not be read as JSON: {type(error).__name__}",
            ),
        )
    validator = _requirements_validator()
    schema_errors = sorted(
        validator.iter_errors(requirements),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if schema_errors:
        error = schema_errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "document"
        return (
            Diagnostic(
                "requirements:schema",
                "blocked",
                f"requirements.json failed schema at {location}: {error.message}",
            ),
        )
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


def _requirements_validator() -> Draft202012Validator:
    schema_resource = resources.files("hoh").joinpath(
        "resources", "schemas", "requirements.schema.json"
    )
    try:
        schema = json.loads(schema_resource.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as error:
        raise ConfigError("packaged requirements schema is unavailable or invalid") from error
    return Draft202012Validator(schema)


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
