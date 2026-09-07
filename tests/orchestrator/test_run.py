import json
from pathlib import Path

import pytest

from hoh.backends import (
    BackendProcessError,
    BackendProtocolError,
    BackendTimeout,
    FakeAgentBackend,
    FakeResponse,
)
from hoh.models import Role, Sandbox
from hoh.orchestrator import PreflightError, RoleOutputError
from hoh.state.evidence import EvidenceBindingError
from hoh.vcs.git import ProtectedPathError

from tests.orchestrator.helpers import (
    BlockedAdapter,
    RecordingAdapter,
    ScriptedBackend,
    build_services,
    count_candidate_commits,
    developer_change,
    developer_response,
    initialized_product,
    orchestrator_fixture,
    plan,
    qa_response,
    run_git,
)


def test_one_loop_calls_roles_in_order_and_freezes_qa(tmp_path: Path) -> None:
    project, services = orchestrator_fixture(tmp_path)

    result = services.orchestrator.run(max_loops=1)

    requests = services.backend.requests
    assert [request.role for request in requests] == [
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
    ]
    assert [request.sandbox for request in requests] == [
        Sandbox.READ_ONLY,
        Sandbox.WORKSPACE_WRITE,
        Sandbox.READ_ONLY,
    ]
    assert requests[2].workspace != project
    assert result["loops_completed"] == 1
    assert (project / ".hoh" / "runs" / result["run_id"]).exists()


def test_schema_invalid_role_output_gets_one_fresh_repair_attempt(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse({"iteration": 1}),
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=1)

    assert result["loops_completed"] == 1
    assert [request.role for request in backend.requests] == [
        Role.PLANNER,
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
    ]
    loop_dir = project / ".hoh" / "runs" / result["run_id"] / "loops" / "loop-0001"
    assert (loop_dir / "planner-events-attempt-01.jsonl").is_file()
    assert (loop_dir / "planner-events-attempt-02.jsonl").is_file()
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((loop_dir / "receipts").glob("planner-*.json"))
    ]
    assert [receipt["outcome"] for receipt in receipts] == [
        "schema_invalid",
        "success",
    ]


@pytest.mark.parametrize(
    "failure",
    [BackendTimeout("temporary timeout"), BackendProcessError("temporary process error")],
)
def test_transient_backend_failure_gets_one_repair_retry(
    tmp_path: Path, failure: BaseException
) -> None:
    project = initialized_product(tmp_path)
    backend = ScriptedBackend(
        [
            failure,
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=1)

    assert result["loops_completed"] == 1
    assert [request.role for request in backend.requests[:2]] == [
        Role.PLANNER,
        Role.PLANNER,
    ]


def test_second_retryable_failure_is_terminal_and_never_runs_a_third_attempt(
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

    assert len(backend.requests) == 2
    run_dir = next((project / ".hoh" / "runs").iterdir())
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "blocked"
    assert state["failure"]["repairable"] is False
    assert (run_dir / "run-summary.md").is_file()


def test_missing_adapter_tool_fails_closed_before_any_role(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend([])
    services = build_services(project, backend, BlockedAdapter())

    with pytest.raises(PreflightError, match="Required executable is missing"):
        services.orchestrator.run(max_loops=1)

    assert backend.requests == []
    run_dir = next((project / ".hoh" / "runs").iterdir())
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "blocked"
    assert state["failure"]["category"] == "infrastructure"
    assert not (project / ".hoh" / "lock").exists()


def test_developer_protected_path_change_is_rejected_before_candidate_commit(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)

    def change_product_and_host_state(request) -> None:
        developer_change(request)
        (request.workspace / ".hoh" / "prd.md").write_text(
            "malicious change\n", encoding="utf-8"
        )

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=change_product_and_host_state),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(ProtectedPathError, match=r"\.hoh"):
        services.orchestrator.run(max_loops=1)

    assert count_candidate_commits(project) == 0
    assert not (project / ".hoh" / "lock").exists()
    run_dir = next((project / ".hoh" / "runs").iterdir())
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "blocked"
    assert state["failure"]["category"] == "protocol"


def test_qa_observes_detached_candidate_and_worktree_is_removed(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    observed: dict[str, object] = {}

    def inspect_frozen_candidate(request) -> dict[str, object]:
        observed["workspace"] = request.workspace
        observed["head"] = run_git(request.workspace, "rev-parse", "HEAD")
        observed["branch"] = run_git(request.workspace, "branch", "--show-current")
        observed["product"] = (request.workspace / "product.txt").read_text(encoding="utf-8")
        return qa_response(request)

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(inspect_frozen_candidate),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=1)

    assert observed["head"] == result["current_candidate"]
    assert observed["branch"] == ""
    assert observed["product"] == "changed\n"
    assert not Path(observed["workspace"]).exists()  # type: ignore[arg-type]
    assert not any((project / ".hoh" / "tmp").glob("qa-*"))


def test_qa_cannot_move_the_frozen_candidate_head(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)

    def commit_inside_frozen_candidate(request) -> None:
        run_git(
            request.workspace,
            "-c",
            "user.name=malicious-qa",
            "-c",
            "user.email=malicious@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "move detached head",
        )

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response, on_run=commit_inside_frozen_candidate),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(RoleOutputError, match="candidate|HEAD|read-only"):
        services.orchestrator.run(max_loops=1)

    assert not any((project / ".hoh" / "tmp").glob("qa-*"))


def test_evidence_for_another_candidate_fails_and_still_cleans_qa_worktree(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)

    def mismatched_evidence(request) -> dict[str, object]:
        return qa_response(request, candidate_sha="b" * 40)

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(mismatched_evidence),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(EvidenceBindingError, match="candidate_sha"):
        services.orchestrator.run(max_loops=1)

    assert count_candidate_commits(project) == 1
    assert not any((project / ".hoh" / "tmp").glob("qa-*"))
    assert not (project / ".hoh" / "lock").exists()
    state_path = next((project / ".hoh" / "runs").glob("*/run.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "blocked"
    assert state["failure"]["category"] == "protocol"


def test_candidate_and_evidence_are_separate_commits_and_receipt_keeps_version(
    tmp_path: Path,
) -> None:
    project, services = orchestrator_fixture(tmp_path)

    result = services.orchestrator.run(max_loops=1)

    candidate = result["current_candidate"]
    evidence_commit = services.git.head_sha()
    assert candidate != evidence_commit
    assert run_git(project, "show", "-s", "--format=%s", str(candidate)).startswith(
        "feat(loop-0001):"
    )
    assert run_git(project, "show", "-s", "--format=%s", evidence_commit) == (
        "test(loop-0001): record candidate evidence"
    )
    assert run_git(project, "rev-parse", f"{evidence_commit}^") == candidate
    receipt_path = (
        project
        / ".hoh"
        / "runs"
        / result["run_id"]
        / "loops"
        / "loop-0001"
        / "receipt.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert [attempt["role"] for attempt in receipt["attempts"]] == [
        "planner",
        "developer",
        "qa",
    ]
    assert {attempt["executable_version"] for attempt in receipt["attempts"]} == {
        "fake-agent/1"
    }


def test_two_loop_bound_runs_fresh_roles_and_stops_after_second_closure(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)

    def change_for(loop_index: int):
        return lambda request: developer_change(request, f"changed-{loop_index}\n")

    backend = FakeAgentBackend(
        [
            FakeResponse(plan(1)),
            FakeResponse(developer_response("loop one"), on_run=change_for(1)),
            FakeResponse(lambda request: qa_response(request, iteration=1)),
            FakeResponse(plan(2)),
            FakeResponse(developer_response("loop two"), on_run=change_for(2)),
            FakeResponse(lambda request: qa_response(request, iteration=2)),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=2)

    assert result["loops_completed"] == 2
    assert result["terminal_status"] == "budget_exhausted"
    assert [request.role for request in backend.requests] == [
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
    ]
    assert count_candidate_commits(project) == 2
    assert services.adapter.check_calls == 2


def test_completion_requires_distinct_full_release_qa_and_fresh_check(
    tmp_path: Path,
) -> None:
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

    result = services.orchestrator.run(max_loops=1)

    assert result["terminal_status"] == "complete"
    assert result["best_candidate"] == result["current_candidate"]
    assert [request.role for request in backend.requests] == [
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
        Role.QA,
    ]
    assert "FULL RELEASE" in backend.requests[3].prompt
    assert backend.requests[3].sandbox is Sandbox.READ_ONLY
    assert services.adapter.check_calls == 2
    loop_record_path = (
        project
        / ".hoh"
        / "runs"
        / result["run_id"]
        / "loops"
        / "loop-0001"
        / "loop-record.json"
    )
    loop_record = json.loads(loop_record_path.read_text(encoding="utf-8"))
    gate = loop_record["release_gate"]
    assert gate["invocation_id"] != loop_record["qa_invocation_id"]
    assert gate["candidate_sha"] == result["current_candidate"]
    assert gate["scope"] == "full_release"
    assert gate["qa_status"] == "pass"
    assert gate["end_to_end_passed"] is True
    assert gate["deterministic_checks_passed"] is True
    assert gate["check_id"]


def test_schema_failure_is_not_repaired_more_than_once(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse({"iteration": 1}),
            FakeResponse({"iteration": 1}),
            FakeResponse(plan()),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(RoleOutputError):
        services.orchestrator.run(max_loops=1)

    assert len(backend.requests) == 2


def test_developer_retry_refreshes_protected_snapshot_after_host_attempt_records(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse({}, on_run=lambda request: developer_change(request, "first\n")),
            FakeResponse(
                developer_response(),
                on_run=lambda request: developer_change(request, "second\n"),
            ),
            FakeResponse(qa_response),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=1)

    assert result["loops_completed"] == 1
    assert [request.role for request in backend.requests] == [
        Role.PLANNER,
        Role.DEVELOPER,
        Role.DEVELOPER,
        Role.QA,
    ]
    assert (project / "product.txt").read_text(encoding="utf-8") == "second\n"


def test_backend_protocol_failure_is_not_retried(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = ScriptedBackend(
        [BackendProtocolError("missing final response"), FakeResponse(plan())]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(BackendProtocolError, match="missing final response"):
        services.orchestrator.run(max_loops=1)

    assert len(backend.requests) == 1
    state_path = next((project / ".hoh" / "runs").glob("*/run.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "blocked"
    assert state["failure"]["category"] == "protocol"


def test_planner_write_attempt_is_rejected_before_developer_runs(tmp_path: Path) -> None:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(
                plan(),
                on_run=lambda request: developer_change(request, "planner-write\n"),
            )
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    with pytest.raises(RoleOutputError, match="read-only"):
        services.orchestrator.run(max_loops=1)

    assert [request.role for request in backend.requests] == [Role.PLANNER]
    assert count_candidate_commits(project) == 0


def test_evidence_commit_retains_every_cited_adapter_artifact(tmp_path: Path) -> None:
    project, services = orchestrator_fixture(tmp_path)

    result = services.orchestrator.run(max_loops=1)

    evidence_path = (
        f".hoh/runs/{result['run_id']}/loops/loop-0001/adapter/artifacts/proof.txt"
    )
    run_git(project, "cat-file", "-e", f"HEAD:{evidence_path}")
    evidence = json.loads(
        (
            project
            / ".hoh"
            / "runs"
            / result["run_id"]
            / "loops"
            / "loop-0001"
            / "evidence.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence["verified_records"][0]["execution_records"][0]["path"] == (
        "adapter/artifacts/proof.txt"
    )


def test_evidence_commit_excludes_raw_role_response_scratch_files(tmp_path: Path) -> None:
    project, services = orchestrator_fixture(tmp_path)

    result = services.orchestrator.run(max_loops=1)

    response_root = f".hoh/runs/{result['run_id']}/loops/loop-0001/responses"
    tracked = run_git(project, "ls-tree", "-r", "--name-only", "HEAD")
    assert not any(path.startswith(response_root + "/") for path in tracked.splitlines())


def test_completion_snapshot_has_distinct_candidate_bound_check_identities(
    tmp_path: Path,
) -> None:
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

    result = services.orchestrator.run(max_loops=1)

    loop_record = json.loads(
        (
            project
            / ".hoh"
            / "runs"
            / result["run_id"]
            / "loops"
            / "loop-0001"
            / "loop-record.json"
        ).read_text(encoding="utf-8")
    )
    assert loop_record["deterministic_check_id"]
    assert loop_record["deterministic_checks_candidate_sha"] == result["current_candidate"]
    assert loop_record["release_gate"]["check_id"] != loop_record["deterministic_check_id"]
