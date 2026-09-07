import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh.backends import BackendTimeout, FakeAgentBackend, FakeResponse
from hoh.models import Role
from hoh.policy import StopPolicy
from hoh.state.evidence import EvidenceBindingError
from hoh.state.store import StateConflictError

from tests.orchestrator.helpers import (
    count_candidate_commits,
    crashed_after_candidate_fixture,
    RecordingAdapter,
    ScriptedBackend,
    build_services,
    config_for,
    developer_change,
    developer_response,
    initialized_product,
    orchestrator_fixture,
    plan,
    qa_response,
    run_git,
)


def test_resume_does_not_repeat_completed_developer(tmp_path: Path) -> None:
    project, services = crashed_after_candidate_fixture(tmp_path)
    original_candidate = services.git.head_sha()
    assert not any((project / ".hoh" / "tmp").glob("qa-*"))

    result = services.orchestrator.resume()

    assert [request.role for request in services.backend.requests] == [Role.QA]
    assert result["current_candidate"] == original_candidate
    assert count_candidate_commits(project) == 1
    assert not any((project / ".hoh" / "tmp").glob("qa-*"))


def test_resume_rejects_tampered_completed_candidate_metadata(tmp_path: Path) -> None:
    project, services = crashed_after_candidate_fixture(tmp_path)
    candidate_path = next((project / ".hoh" / "runs").glob("*/loops/loop-0001/candidate.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["candidate_sha"] = "b" * 40
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(StateConflictError, match="candidate"):
        services.orchestrator.resume()

    assert services.backend.requests == []
    assert count_candidate_commits(project) == 1


def test_resume_recovers_candidate_commit_that_landed_before_metadata(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def land_then_crash(prepared) -> str:
        result = land(prepared)
        if prepared.kind == "candidate":
            raise RuntimeError("crash after candidate commit")
        return result

    services.git.land_prepared_commit = land_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after candidate commit"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    services.backend.requests.clear()
    landed_candidate = services.git.head_sha()

    result = services.orchestrator.resume()

    assert [request.role for request in services.backend.requests] == [Role.QA]
    assert result["current_candidate"] == landed_candidate
    assert count_candidate_commits(project) == 1


def test_resume_rejects_external_direct_child_in_candidate_crash_window(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def crash_before_land(prepared) -> str:
        raise RuntimeError(f"crash before landing {prepared.kind}")

    services.git.land_prepared_commit = crash_before_land  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before landing candidate"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    backend.requests.clear()
    run_git(
        project,
        "-c",
        "user.name=external",
        "-c",
        "user.email=external@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "unexpected empty candidate",
    )

    with pytest.raises(StateConflictError, match="candidate|prepared|intent|HEAD"):
        services.orchestrator.resume()

    assert backend.requests == []
    assert (project / "product.txt").read_text(encoding="utf-8") == "changed\n"


def test_resume_rejects_candidate_with_expected_tree_but_wrong_identity(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def land_then_crash(prepared) -> str:
        landed = land(prepared)
        raise RuntimeError(f"crash after landing {prepared.kind}: {landed}")

    services.git.land_prepared_commit = land_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after landing candidate"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    backend.requests.clear()
    run_git(
        project,
        "-c",
        "user.name=external",
        "-c",
        "user.email=external@example.test",
        "commit",
        "--amend",
        "--no-edit",
        "--author=external <external@example.test>",
    )

    with pytest.raises(StateConflictError, match="candidate|prepared|intent|HEAD"):
        services.orchestrator.resume()

    assert backend.requests == []


def test_resume_recovers_evidence_commit_that_landed_before_closure_journal(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def land_then_crash(prepared) -> str:
        result = land(prepared)
        if prepared.kind == "evidence":
            raise RuntimeError("crash after evidence commit")
        return result

    services.git.land_prepared_commit = land_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after evidence commit"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    services.backend.requests.clear()
    evidence_head = services.git.head_sha()

    result = services.orchestrator.resume()

    assert services.backend.requests == []
    assert services.git.head_sha() == evidence_head
    messages = run_git(project, "log", "--format=%s").splitlines()
    assert sum(message.startswith("test(loop-") for message in messages) == 1
    assert result["loops_completed"] == 1


def test_resume_rejects_partial_external_evidence_commit_in_crash_window(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def crash_before_evidence_land(prepared) -> str:
        if prepared.kind == "evidence":
            raise RuntimeError("crash before landing evidence")
        return land(prepared)

    services.git.land_prepared_commit = crash_before_evidence_land  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before landing evidence"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    backend.requests.clear()
    evidence = next(
        (project / ".hoh" / "runs").glob("*/loops/loop-0001/evidence.json")
    )
    run_git(project, "add", "--", str(evidence.relative_to(project)))
    run_git(
        project,
        "-c",
        "user.name=external",
        "-c",
        "user.email=external@example.test",
        "commit",
        "-m",
        "partial external evidence",
    )

    with pytest.raises(StateConflictError, match="evidence|prepared|intent|HEAD"):
        services.orchestrator.resume()

    assert backend.requests == []
    tracked = run_git(project, "ls-tree", "-r", "--name-only", "HEAD")
    assert "receipt.json" not in tracked


def test_resume_never_grants_more_than_two_total_role_attempts(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = ScriptedBackend(
        [
            BackendTimeout("first timeout"),
            BackendTimeout("second timeout"),
            FakeResponse(plan()),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(BackendTimeout, match="second timeout"):
        services.orchestrator.run(max_loops=1)
    run_path = next((project / ".hoh" / "runs").glob("*/run.json"))
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state["status"] = "resumable"
    run_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(StateConflictError, match="attempt|retry|receipt"):
        services.orchestrator.resume()

    assert len(backend.requests) == 2
    receipts = sorted(run_path.parent.glob("loops/loop-0001/receipts/planner-*.json"))
    assert len(receipts) == 2


def test_resume_fails_closed_on_malformed_attempt_receipt_history(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = ScriptedBackend(
        [
            BackendTimeout("first timeout"),
            RuntimeError("simulated process crash"),
            FakeResponse(plan()),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(RuntimeError, match="simulated process crash"):
        services.orchestrator.run(max_loops=1)
    run_path = next((project / ".hoh" / "runs").glob("*/run.json"))
    receipt_path = next(
        run_path.parent.glob("loops/loop-0001/receipts/planner-attempt-01.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["attempt"] = 7
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(StateConflictError, match="attempt|receipt"):
        services.orchestrator.resume()

    assert len(backend.requests) == 2


def test_elapsed_time_rebases_once_when_a_later_loop_resumes(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)

    class Clock:
        elapsed = 0.0
        epoch = datetime(2026, 1, 1, tzinfo=UTC)

        def monotonic(self) -> float:
            return self.elapsed

        def now(self) -> datetime:
            return self.epoch + timedelta(seconds=self.elapsed)

        def advance(self, seconds: float) -> None:
            self.elapsed += seconds

    class Policy:
        def __init__(self, delegate: StopPolicy) -> None:
            self.delegate = delegate
            self.elapsed: list[int] = []

        def evaluate(self, history, **kwargs):
            self.elapsed.append(kwargs.get("elapsed_seconds", 0))
            return self.delegate.evaluate(history, **kwargs)

    clock = Clock()
    config = config_for(project)
    policy = Policy(StopPolicy(config))

    def change_for(index: int):
        def change(request) -> None:
            developer_change(request, f"changed-{index}\n")
            clock.advance(30)

        return change

    def qa_for(index: int):
        def respond(request):
            clock.advance(30)
            return qa_response(request, iteration=index)

        return respond

    backend = ScriptedBackend(
        [
            FakeResponse(plan(1)),
            FakeResponse(developer_response("loop one"), on_run=change_for(1)),
            FakeResponse(qa_for(1)),
            BackendTimeout("pause before retry"),
            FakeResponse(plan(2)),
            FakeResponse(developer_response("loop two"), on_run=change_for(2)),
            FakeResponse(qa_for(2)),
        ]
    )
    services = build_services(
        project,
        backend,
        RecordingAdapter(),
        config=config,
        policy=policy,  # type: ignore[arg-type]
        orchestrator_options={"now": clock.now, "monotonic": clock.monotonic},
    )
    repair_prompt = services.orchestrator._repair_prompt
    services.orchestrator._repair_prompt = (  # type: ignore[method-assign]
        lambda prompt, error: (_ for _ in ()).throw(
            RuntimeError("simulated process loss before retry")
        )
    )

    with pytest.raises(RuntimeError, match="process loss"):
        services.orchestrator.run(max_loops=2)
    services.orchestrator._repair_prompt = repair_prompt  # type: ignore[method-assign]

    result = services.orchestrator.resume()

    assert result["loops_completed"] == 2
    assert policy.elapsed == [60, 120]
    state = json.loads(
        (
            project / ".hoh" / "runs" / result["run_id"] / "run.json"
        ).read_text(encoding="utf-8")
    )
    assert state["elapsed_seconds"] == 120


def test_resume_rejects_tampered_release_gate_check_record(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(lambda request: qa_response(request, include_major_gap=False)),
            FakeResponse(lambda request: qa_response(request, include_major_gap=False)),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def crash_before_evidence(prepared) -> str:
        if prepared.kind == "evidence":
            raise RuntimeError("crash before evidence commit")
        return land(prepared)

    services.git.land_prepared_commit = crash_before_evidence  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before evidence commit"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    services.backend.requests.clear()
    release_checks = next((project / ".hoh" / "runs").glob("*/loops/loop-0001/release-checks.json"))
    release_checks.write_text("{}\n", encoding="utf-8")

    with pytest.raises(StateConflictError, match="release|checks"):
        services.orchestrator.resume()

    assert services.backend.requests == []


def test_resume_revalidates_cited_evidence_files_before_closure(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())
    land = services.git.land_prepared_commit

    def crash_before_evidence(prepared) -> str:
        if prepared.kind == "evidence":
            raise RuntimeError("crash before evidence commit")
        return land(prepared)

    services.git.land_prepared_commit = crash_before_evidence  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before evidence commit"):
        services.orchestrator.run(max_loops=1)
    services.git.land_prepared_commit = land  # type: ignore[method-assign]
    services.backend.requests.clear()
    proof = next(
        (project / ".hoh" / "runs").glob(
            "*/loops/loop-0001/adapter/artifacts/proof.txt"
        )
    )
    proof.write_text("tampered\n", encoding="utf-8")

    with pytest.raises((EvidenceBindingError, StateConflictError), match="sha256|evidence"):
        services.orchestrator.resume()

    assert services.backend.requests == []


def test_resume_rejects_head_moved_after_completed_closure(tmp_path: Path) -> None:
    project, services = orchestrator_fixture(tmp_path)
    record_closed_loop = services.orchestrator._record_closed_loop
    services.orchestrator._record_closed_loop = (  # type: ignore[method-assign]
        lambda *args: (_ for _ in ()).throw(RuntimeError("crash after closure"))
    )
    with pytest.raises(RuntimeError, match="crash after closure"):
        services.orchestrator.run(max_loops=1)
    services.orchestrator._record_closed_loop = record_closed_loop  # type: ignore[method-assign]
    services.backend.requests.clear()
    run_git(
        project,
        "-c",
        "user.name=external",
        "-c",
        "user.email=external@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "unexpected post-closure commit",
    )

    with pytest.raises(StateConflictError, match="closure|evidence|HEAD"):
        services.orchestrator.resume()

    assert services.backend.requests == []
