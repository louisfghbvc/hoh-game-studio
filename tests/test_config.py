from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import json
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10.
    import tomli as tomllib

import pytest

from hoh.cli import build_parser, main
from hoh.config import ConfigError, doctor, initialize_project, load_config
from hoh.models import Diagnostic


PROJECT_ROOT = Path(__file__).parents[1]


def git_init(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)


def test_init_writes_explicit_reproducible_config(tmp_path: Path) -> None:
    git_init(tmp_path)

    initialize_project(tmp_path, "command", "test-model", "high")

    config = load_config(tmp_path)
    assert config.model == "test-model"
    assert config.reasoning_effort == "high"
    assert config.max_loops == 12
    assert config.protected_paths == (".hoh", ".git")
    assert config.max_total_tokens == 5_000_000
    assert config.max_elapsed_minutes == 480


def test_init_rejects_missing_model(tmp_path: Path) -> None:
    git_init(tmp_path)

    with pytest.raises(ConfigError, match="model is required"):
        initialize_project(tmp_path, "command", "", "high")


def test_init_writes_required_state_without_overwriting_existing_content(
    tmp_path: Path,
) -> None:
    git_init(tmp_path)
    state = tmp_path / ".hoh"
    state.mkdir()
    prd = state / "prd.md"
    prd.write_text("Keep this PRD.", encoding="utf-8")

    config_path = initialize_project(tmp_path, "godot", "test-model", "medium")

    assert config_path == state / "config.toml"
    assert prd.read_text(encoding="utf-8") == "Keep this PRD."
    assert json.loads((state / "requirements.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "claims": [],
    }
    assert json.loads((state / "issue-ledger.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "issues": [],
    }
    assert (state / "runs").is_dir()


def test_init_requires_a_git_repository(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Git repository"):
        initialize_project(tmp_path, "command", "test-model", "high")


@pytest.mark.parametrize(
    ("adapter", "model", "reasoning_effort", "message"),
    [
        ("unknown", "test-model", "high", "unknown adapter"),
        ("command", "test-model", "", "reasoning_effort is required"),
    ],
)
def test_init_rejects_invalid_required_values(
    tmp_path: Path,
    adapter: str,
    model: str,
    reasoning_effort: str,
    message: str,
) -> None:
    git_init(tmp_path)

    with pytest.raises(ConfigError, match=message):
        initialize_project(tmp_path, adapter, model, reasoning_effort)


def test_load_config_rejects_nonpositive_limits(tmp_path: Path) -> None:
    git_init(tmp_path)
    initialize_project(tmp_path, "command", "test-model", "high")
    config_path = tmp_path / ".hoh" / "config.toml"
    config_path.write_text(config_path.read_text(encoding="utf-8").replace("max_loops = 12", "max_loops = 0"), encoding="utf-8")

    with pytest.raises(ConfigError, match="max_loops must be a positive integer"):
        load_config(tmp_path)


def test_doctor_blocks_until_a_required_claim_exists(tmp_path: Path) -> None:
    git_init(tmp_path)
    initialize_project(tmp_path, "command", "test-model", "high")

    diagnostics = doctor(load_config(tmp_path))

    assert Diagnostic(
        "requirements:required-claim",
        "blocked",
        "requirements.json must contain at least one required claim",
    ) in diagnostics


def test_doctor_accepts_a_registry_with_a_required_claim(tmp_path: Path) -> None:
    git_init(tmp_path)
    initialize_project(tmp_path, "command", "test-model", "high")
    requirements = tmp_path / ".hoh" / "requirements.json"
    requirements.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claims": [
                    {"id": "boot", "description": "The product boots.", "required": True}
                ],
            }
        ),
        encoding="utf-8",
    )

    assert doctor(load_config(tmp_path)) == ()


def test_domain_records_are_immutable() -> None:
    diagnostic = Diagnostic("config", "info", "ready")

    with pytest.raises(FrozenInstanceError):
        diagnostic.code = "changed"  # type: ignore[misc]


def test_parser_defines_init_and_doctor_commands() -> None:
    parser = build_parser()

    init = parser.parse_args(
        [
            "init",
            "--adapter",
            "command",
            "--model",
            "test-model",
            "--reasoning-effort",
            "high",
        ]
    )
    doctor_arguments = parser.parse_args(["doctor"])

    assert init.adapter == "command"
    assert init.model == "test-model"
    assert init.reasoning_effort == "high"
    assert doctor_arguments.project == "."


def test_init_command_creates_project_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git_init(tmp_path)

    assert main(
        [
            "init",
            "--project",
            str(tmp_path),
            "--adapter",
            "command",
            "--model",
            "test-model",
            "--reasoning-effort",
            "high",
        ]
    ) == 0
    assert (tmp_path / ".hoh" / "config.toml").is_file()
    assert "Initialized HoH project" in capsys.readouterr().out


def test_doctor_command_returns_blocked_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_init(tmp_path)
    initialize_project(tmp_path, "command", "test-model", "high")

    assert main(["doctor", "--project", str(tmp_path)]) == 3
    assert "requirements:required-claim" in capsys.readouterr().out


def test_python310_installs_tomli_parser_dependency() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "tomli>=2.0,<3; python_version < '3.11'" in pyproject["project"]["dependencies"]


def test_config_imports_tomli_when_tomllib_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "tomli.py").write_text(
        "class TOMLDecodeError(ValueError):\n    pass\n\n"
        "def load(file):\n    return {}\n",
        encoding="utf-8",
    )
    script = """
import builtins

real_import = builtins.__import__

def import_without_tomllib(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "tomllib":
        raise ModuleNotFoundError("No module named 'tomllib'")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = import_without_tomllib
import hoh.config as config
assert config.tomllib.__name__ == "tomli"
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), str(PROJECT_ROOT / "src"), environment.get("PYTHONPATH", "")]
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_load_config_recursively_freezes_adapter_options(tmp_path: Path) -> None:
    git_init(tmp_path)
    initialize_project(tmp_path, "command", "test-model", "high")
    config_path = tmp_path / ".hoh" / "config.toml"
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write(
            "\n[adapter_options.runner]\n"
            'command = ["python", "-m", "pytest"]\n'
        )

    config = load_config(tmp_path)
    runner = config.adapter_options["runner"]

    assert runner["command"] == ("python", "-m", "pytest")  # type: ignore[index]
    with pytest.raises(TypeError):
        runner["command"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        runner["command"][0] = "changed"  # type: ignore[index]
