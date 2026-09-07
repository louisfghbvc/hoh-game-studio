from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import hoh.cli as cli
from hoh.backends.base import BackendProtocolError
from hoh.backends.fake import FakeAgentBackend
from hoh.config import ConfigError, initialize_project, load_config
from hoh.orchestrator import PreflightError
from hoh.prompts import PromptRenderingError
from hoh.state.evidence import EvidenceBindingError
from hoh.state.store import RunLockedError, StateConflictError
from hoh.vcs.git import ProtectedPathError


def _git(project: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _initialized_project(
    tmp_path: Path,
    *,
    adapter: str = "command",
    adapter_options: str = "",
) -> Path:
    project = tmp_path / "product"
    project.mkdir()
    _git(project, "init", "--initial-branch=main")
    (project / "product.txt").write_text("original\n", encoding="utf-8")
    initialize_project(project, adapter, "offline-model", "low")
    (project / ".hoh" / "requirements.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claims": [
                    {
                        "id": "claim-main",
                        "description": "Main behavior works",
                        "required": True,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if adapter_options:
        with (project / ".hoh" / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(adapter_options)
    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "initial",
    )
    return project


def _write_run(
    project: Path,
    run_id: str,
    *,
    status: str,
    updated_at: str,
    candidate: str | None = None,
) -> Path:
    run_dir = project / ".hoh" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    loops: list[dict[str, object]] = []
    if candidate is not None:
        loops.append(
            {
                "loop_index": 1,
                "candidate_sha": candidate,
                "normalized_evidence": {
                    "candidate_sha": candidate,
                    "product_complete": status == "complete",
                    "verified_records": [],
                },
            }
        )
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "start_sha": _git(project, "rev-parse", "HEAD"),
        "status": status,
        "reason": "fixture decision",
        "updated_at": updated_at,
        "current_candidate": candidate,
        "best_candidate": candidate if status == "complete" else None,
        "elapsed_seconds": 7,
        "loops": loops,
        "receipts": [],
        "skill_receipts": [],
    }
    (run_dir / "run.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    return run_dir


class _StubOrchestrator:
    def __init__(
        self,
        *,
        result: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or {
            "run_id": "run-new",
            "status": "complete",
            "terminal_status": "complete",
            "reason": "all required claims verified",
        }
        self.error = error
        self.run_calls: list[int | None] = []
        self.resume_calls = 0

    def run(self, max_loops: int | None = None) -> dict[str, object]:
        self.run_calls.append(max_loops)
        if self.error is not None:
            raise self.error
        return dict(self.result)

    def resume(self) -> dict[str, object]:
        self.resume_calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.result)


def _inject_orchestrator(
    monkeypatch: pytest.MonkeyPatch, orchestrator: _StubOrchestrator
) -> None:
    monkeypatch.setattr(
        cli,
        "build_services",
        lambda config, backend_name="codex-exec": SimpleNamespace(
            orchestrator=orchestrator,
            adapter=SimpleNamespace(doctor=lambda project: ()),
        ),
        raising=False,
    )


def test_help_lists_every_public_command_group(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0

    output = capsys.readouterr()
    assert output.err == ""
    for command in ("init", "doctor", "run", "status", "resume", "report", "skills"):
        assert command in output.out

    assert cli.main(["skills", "--help"]) == 0
    assert "list" in capsys.readouterr().out


def test_argparse_usage_errors_return_two_instead_of_escaping(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["run", "--max-loops", "not-an-integer"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "usage: hoh run" in output.err


def test_public_cli_does_not_expose_fake_backend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["run", "--backend", "fake"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_doctor_returns_blocked_exit_code_for_missing_godot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing-godot"
    project = _initialized_project(
        tmp_path,
        adapter="godot",
        adapter_options=f"command = {json.dumps([str(missing)])}\nproject_subdir = \".\"\n",
    )
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")

    assert cli.main(["doctor", "--project", str(project)]) == 3
    output = capsys.readouterr()
    assert "Godot executable was not found" in output.out
    assert output.err == ""


def test_malformed_adapter_configuration_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _initialized_project(
        tmp_path,
        adapter_options='checks = ["python"]\n',
    )

    assert cli.main(["doctor", "--project", str(project)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "adapter_options.checks" in output.err


def test_status_json_is_machine_readable_and_contains_no_human_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _initialized_project(tmp_path)
    candidate = "a" * 40
    _write_run(
        project,
        "run-complete",
        status="complete",
        updated_at="2026-09-08T01:02:03+00:00",
        candidate=candidate,
    )

    assert cli.main(["status", "--project", str(project), "--json"]) == 0
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert output.err == ""
    assert document["status"] == "complete"
    assert document["guidance"] == f"git merge {candidate}"


def test_status_json_protocol_error_writes_no_partial_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _initialized_project(tmp_path)
    run_dir = _write_run(
        project,
        "run-corrupt",
        status="running",
        updated_at="2026-09-08T01:02:03+00:00",
    )
    (run_dir / "run.json").write_text("not json\n", encoding="utf-8")

    assert cli.main(["status", "--project", str(project), "--json"]) == 5
    output = capsys.readouterr()
    assert output.out == ""
    assert "could not read durable run state" in output.err


def test_report_is_regenerated_from_structured_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _initialized_project(tmp_path)
    run_dir = _write_run(
        project,
        "run-blocked",
        status="blocked",
        updated_at="2026-09-08T01:02:03+00:00",
    )
    (run_dir / "run-summary.md").write_text("untrusted stale prose\n", encoding="utf-8")

    assert cli.main(["report", "--project", str(project)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "Status: blocked" in output.out
    assert "untrusted stale prose" not in output.out
    assert (run_dir / "run-summary.md").read_text(encoding="utf-8") == output.out


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("complete", 0),
        ("budget_exhausted", 4),
        ("blocked", 4),
        ("cancelled", 130),
    ],
)
def test_run_maps_structured_terminal_status_to_stable_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected: int,
) -> None:
    project = _initialized_project(tmp_path)
    orchestrator = _StubOrchestrator(
        result={
            "run_id": "run-new",
            "status": status,
            "terminal_status": status,
            "reason": "fixture decision",
        }
    )
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["run", "--project", str(project), "--max-loops", "2"]) == expected
    output = capsys.readouterr()
    assert output.err == ""
    assert f"Status: {status}" in output.out
    assert orchestrator.run_calls == [2]


@pytest.mark.parametrize(
    ("failure_category", "expected"),
    [("infrastructure", 3), ("protocol", 5)],
)
def test_run_maps_structured_failure_category_to_stable_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_category: str,
    expected: int,
) -> None:
    project = _initialized_project(tmp_path)
    orchestrator = _StubOrchestrator(
        result={
            "run_id": "run-new",
            "status": "blocked",
            "terminal_status": "blocked",
            "reason": f"unrecoverable {failure_category} failure: fixture",
            "failure_category": failure_category,
        }
    )
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["run", "--project", str(project)]) == expected
    output = capsys.readouterr()
    assert output.err == ""
    assert f"Status: blocked" in output.out


def test_run_rejects_nonterminal_structured_result_before_writing_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _initialized_project(tmp_path)
    orchestrator = _StubOrchestrator(
        result={
            "run_id": "run-new",
            "status": "running",
            "terminal_status": "running",
            "reason": "not actually terminal",
        }
    )
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["run", "--project", str(project)]) == 5
    output = capsys.readouterr()
    assert output.out == ""
    assert "unexpected orchestrator terminal status" in output.err


def test_run_rejects_conflicting_structured_status_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _initialized_project(tmp_path)
    orchestrator = _StubOrchestrator(
        result={
            "run_id": "run-new",
            "status": "blocked",
            "terminal_status": "complete",
            "reason": "conflicting fixture",
        }
    )
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["run", "--project", str(project)]) == 5
    output = capsys.readouterr()
    assert output.out == ""
    assert "conflicting structured status" in output.err


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PreflightError("adapter prerequisite is blocked"), 3),
        (RunLockedError("run lock already exists"), 3),
        (StateConflictError("durable state conflicts"), 5),
        (ProtectedPathError("developer touched protected state"), 5),
        (EvidenceBindingError("evidence is not candidate-bound"), 5),
        (BackendProtocolError("backend protocol is malformed"), 5),
        (PromptRenderingError("packaged prompt could not render"), 3),
        (KeyboardInterrupt(), 130),
    ],
)
def test_run_maps_exception_category_and_writes_errors_only_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    expected: int,
) -> None:
    project = _initialized_project(tmp_path)
    orchestrator = _StubOrchestrator(error=error)
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["run", "--project", str(project)]) == expected
    output = capsys.readouterr()
    assert output.out == ""
    if expected != 130 or str(error):
        assert str(error) in output.err


def test_run_surfaces_stale_lock_without_removing_or_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _initialized_project(tmp_path)
    lock = project / ".hoh" / "lock"
    lock.write_text('{"pid": 999999}\n', encoding="utf-8")
    orchestrator = _StubOrchestrator()
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["run", "--project", str(project)]) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "run lock already exists" in output.err
    assert lock.is_file()
    assert orchestrator.run_calls == []


def test_resume_run_id_must_match_newest_durable_resumable_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _initialized_project(tmp_path)
    _write_run(
        project,
        "run-old",
        status="resumable",
        updated_at="2026-09-08T01:00:00+00:00",
    )
    _write_run(
        project,
        "run-new",
        status="running",
        updated_at="2026-09-08T02:00:00+00:00",
    )
    orchestrator = _StubOrchestrator(
        result={
            "run_id": "run-new",
            "status": "budget_exhausted",
            "terminal_status": "budget_exhausted",
            "reason": "loop budget exhausted",
        }
    )
    _inject_orchestrator(monkeypatch, orchestrator)

    assert (
        cli.main(["resume", "--project", str(project), "--run-id", "run-old"])
        == 2
    )
    rejected = capsys.readouterr()
    assert rejected.out == ""
    assert "newest resumable run is run-new" in rejected.err
    assert orchestrator.resume_calls == 0

    assert (
        cli.main(["resume", "--project", str(project), "--run-id", "run-new"])
        == 4
    )
    accepted = capsys.readouterr()
    assert accepted.err == ""
    assert "Status: budget_exhausted" in accepted.out
    assert orchestrator.resume_calls == 1


def test_resume_without_run_id_delegates_selection_to_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _initialized_project(tmp_path)
    orchestrator = _StubOrchestrator()
    _inject_orchestrator(monkeypatch, orchestrator)

    assert cli.main(["resume", "--project", str(project)]) == 0
    assert orchestrator.resume_calls == 1


def test_resume_run_id_is_not_silently_discarded_if_selected_run_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _initialized_project(tmp_path)
    _write_run(
        project,
        "run-new",
        status="resumable",
        updated_at="2026-09-08T02:00:00+00:00",
    )
    orchestrator = _StubOrchestrator(
        result={
            "run_id": "run-different",
            "status": "complete",
            "terminal_status": "complete",
            "reason": "selection changed",
        }
    )
    _inject_orchestrator(monkeypatch, orchestrator)

    assert (
        cli.main(["resume", "--project", str(project), "--run-id", "run-new"])
        == 5
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "resumed run does not match --run-id" in output.err
    assert orchestrator.resume_calls == 1


def test_skills_list_uses_packaged_registry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["skills", "list"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "core.bounded-planning" in output.out
    assert "core.evidence-grounded-qa" in output.out
    assert "godot.runtime-testing" in output.out


def test_build_services_accepts_only_python_level_fake_injection(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    config = load_config(project)
    backend = FakeAgentBackend([])

    services = cli.build_services(config, backend_name=backend)

    assert services.backend is backend
    assert services.orchestrator.backend is backend
    assert services.git.repository == project.resolve()
    assert services.orchestrator.schema_dir.name == "schemas"
    with pytest.raises(ConfigError, match="unknown backend"):
        cli.build_services(config, backend_name="fake")
