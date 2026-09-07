from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from hoh.adapters.base import AdapterContext
from hoh.adapters.godot import GodotAdapter
from hoh.models import Diagnostic


@pytest.fixture
def fake_godot() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "fake_godot.py"


def test_missing_godot_is_blocked(tmp_path: Path) -> None:
    """Treating an unavailable Godot binary as a pass must make this fail."""
    adapter = GodotAdapter(command=(str(tmp_path / "missing-godot"),), project_subdir=".")

    diagnostics = adapter.doctor(tmp_path)

    assert diagnostics == (
        Diagnostic("godot:executable", "blocked", "Godot executable was not found"),
    )


def test_fake_godot_runtime_error_fails(tmp_path: Path, fake_godot: Path) -> None:
    """Ignoring SCRIPT ERROR output must make this fail."""
    project = copy_minimal_project(tmp_path)
    (project / "project.godot").write_text(
        (project / "project.godot").read_text(encoding="utf-8") + "\nfake_error = true\n",
        encoding="utf-8",
    )
    adapter = GodotAdapter(command=(sys.executable, str(fake_godot)), project_subdir=".")

    bundle = adapter.check(AdapterContext(project, tmp_path / "out"), {})

    assert bundle.status == "fail"
    runtime_result = next(
        result for result in bundle.results if result.check_id == "godot:runtime-errors"
    )
    assert runtime_result.status == "fail"


def test_default_headless_import_uses_project_and_retains_records(
    tmp_path: Path, fake_godot: Path
) -> None:
    """Dropping the deterministic headless import command must make this fail."""
    project = copy_minimal_project(tmp_path)
    context = AdapterContext(project, tmp_path / "out")
    adapter = GodotAdapter(command=(sys.executable, str(fake_godot)), project_subdir=".")

    bundle = adapter.check(context, {})

    assert bundle.status == "pass"
    assert {result.check_id for result in bundle.results} == {
        "godot:executable",
        "godot:project",
        "godot:headless-import",
        "godot:runtime-errors",
    }
    metadata = json.loads(
        (context.output / "checks" / "command-0001.json").read_text(encoding="utf-8")
    )
    assert metadata["command"][-5:] == [
        "--headless",
        "--path",
        str(project.resolve()),
        "--editor",
        "--quit",
    ]


def test_timeout_is_a_godot_failure(tmp_path: Path, fake_godot: Path) -> None:
    """Converting a Godot timeout to pass or blocked must make this fail."""
    project = copy_minimal_project(tmp_path)
    (project / "project.godot").write_text(
        (project / "project.godot").read_text(encoding="utf-8") + "\nfake_sleep = true\n",
        encoding="utf-8",
    )
    adapter = GodotAdapter(
        command=(sys.executable, str(fake_godot)), project_subdir=".", timeout_seconds=1
    )

    bundle = adapter.check(AdapterContext(project, tmp_path / "out"), {})

    headless = next(
        result for result in bundle.results if result.check_id == "godot:headless-import"
    )
    assert bundle.status == "fail"
    assert headless.status == "fail"
    assert headless.summary == "Command timed out after 1 seconds"


def test_missing_project_configuration_fails_before_running_godot(
    tmp_path: Path, fake_godot: Path
) -> None:
    """Running a non-Godot directory must make this fail."""
    adapter = GodotAdapter(command=(sys.executable, str(fake_godot)), project_subdir=".")

    bundle = adapter.check(AdapterContext(tmp_path, tmp_path.parent / "out"), {})

    assert bundle.status == "fail"
    assert [(result.check_id, result.status) for result in bundle.results] == [
        ("godot:executable", "pass"),
        ("godot:project", "fail"),
    ]


def test_crash_and_configured_replay_are_separate_failed_checks(
    tmp_path: Path, fake_godot: Path
) -> None:
    """Treating crashes or replay commands as optional must make this fail."""
    project = copy_minimal_project(tmp_path)
    (project / "project.godot").write_text(
        (project / "project.godot").read_text(encoding="utf-8") + "\nfake_crash = true\n",
        encoding="utf-8",
    )
    adapter = GodotAdapter(
        command=(sys.executable, str(fake_godot)),
        project_subdir=".",
        replay_commands=(("--headless", "--path", "{project}", "--quit"),),
    )

    bundle = adapter.check(AdapterContext(project, tmp_path / "out"), {})

    assert bundle.status == "fail"
    assert next(
        result for result in bundle.results if result.check_id == "godot:replay:0001"
    ).status == "fail"


def test_missing_required_evidence_fails_and_matching_evidence_is_collected(
    tmp_path: Path, fake_godot: Path
) -> None:
    """Accepting an unmatched evidence glob must make this fail."""
    project = copy_minimal_project(tmp_path)
    evidence = project / "evidence"
    evidence.mkdir()
    (evidence / "telemetry.jsonl").write_text("event\n", encoding="utf-8")
    context = AdapterContext(project, tmp_path / "out")
    adapter = GodotAdapter(
        command=(sys.executable, str(fake_godot)),
        project_subdir=".",
        required_evidence_globs=("evidence/*.jsonl", "screenshots/*.png"),
    )

    bundle = adapter.check(context, {})

    assert bundle.status == "fail"
    assert next(
        result
        for result in bundle.results
        if result.check_id == "godot:evidence:evidence/*.jsonl"
    ).status == "pass"
    assert next(
        result
        for result in bundle.results
        if result.check_id == "godot:evidence:screenshots/*.png"
    ).status == "fail"
    artifacts = adapter.collect(context, bundle)
    assert set(artifacts) == {"artifacts/evidence/telemetry.jsonl"}


def copy_minimal_project(destination: Path) -> Path:
    project = destination / "minimal-godot"
    shutil.copytree(Path(__file__).parents[2] / "examples" / "minimal-godot", project)
    return project
