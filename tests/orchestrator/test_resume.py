from pathlib import Path

import json
import pytest

from hoh.backends import FakeAgentBackend, FakeResponse
from hoh.models import Role
from hoh.state.evidence import EvidenceBindingError
from hoh.state.store import StateConflictError

from tests.orchestrator.helpers import (
    count_candidate_commits,
    crashed_after_candidate_fixture,
    RecordingAdapter,
    build_services,
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
    commit_candidate = services.git.commit_candidate

    def commit_then_crash(loop_index: int, summary: str) -> str:
        commit_candidate(loop_index, summary)
        raise RuntimeError("crash after candidate commit")

    services.git.commit_candidate = commit_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after candidate commit"):
        services.orchestrator.run(max_loops=1)
    services.git.commit_candidate = commit_candidate  # type: ignore[method-assign]
    services.backend.requests.clear()
    landed_candidate = services.git.head_sha()

    result = services.orchestrator.resume()

    assert [request.role for request in services.backend.requests] == [Role.QA]
    assert result["current_candidate"] == landed_candidate
    assert count_candidate_commits(project) == 1


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
    commit_evidence = services.git.commit_evidence

    def commit_then_crash(loop_index: int, paths: tuple[Path, ...]) -> str:
        commit_evidence(loop_index, paths)
        raise RuntimeError("crash after evidence commit")

    services.git.commit_evidence = commit_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after evidence commit"):
        services.orchestrator.run(max_loops=1)
    services.git.commit_evidence = commit_evidence  # type: ignore[method-assign]
    services.backend.requests.clear()
    evidence_head = services.git.head_sha()

    result = services.orchestrator.resume()

    assert services.backend.requests == []
    assert services.git.head_sha() == evidence_head
    messages = run_git(project, "log", "--format=%s").splitlines()
    assert sum(message.startswith("test(loop-") for message in messages) == 1
    assert result["loops_completed"] == 1


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
    commit_evidence = services.git.commit_evidence
    services.git.commit_evidence = (  # type: ignore[method-assign]
        lambda loop_index, paths: (_ for _ in ()).throw(
            RuntimeError("crash before evidence commit")
        )
    )
    with pytest.raises(RuntimeError, match="crash before evidence commit"):
        services.orchestrator.run(max_loops=1)
    services.git.commit_evidence = commit_evidence  # type: ignore[method-assign]
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
    commit_evidence = services.git.commit_evidence
    services.git.commit_evidence = (  # type: ignore[method-assign]
        lambda loop_index, paths: (_ for _ in ()).throw(
            RuntimeError("crash before evidence commit")
        )
    )
    with pytest.raises(RuntimeError, match="crash before evidence commit"):
        services.orchestrator.run(max_loops=1)
    services.git.commit_evidence = commit_evidence  # type: ignore[method-assign]
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
