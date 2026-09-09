import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
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
from hoh.orchestrator import PreflightError, ResumeError, RoleOutputError
from hoh.policy import StopPolicy
from hoh.state.evidence import EvidenceBindingError
from hoh.state.issue_ledger import IssueLedgerError
from hoh.state.store import RunLockedError, StateConflictError
from hoh.vcs.git import GitError, ProtectedPathError

from tests.orchestrator.helpers import (
    BlockedAdapter,
    RecordingAdapter,
    ScriptedBackend,
    build_services,
    config_for,
    count_candidate_commits,
    crashed_after_candidate_fixture,
    developer_change,
    developer_response,
    initialized_product,
    orchestrator_fixture,
    plan,
    qa_response,
    run_git,
)


class MutableClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.epoch + timedelta(seconds=self.elapsed)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


def test_frozen_candidate_retries_cleanup_after_partial_create_failure(
    tmp_path: Path,
) -> None:
    """A failed create must still enter the orchestrator's cleanup boundary."""

    project = initialized_product(tmp_path)

    class PartialWorktree:
        removed = False

        def create(self, candidate_sha: str) -> Path:
            del candidate_sha
            raise GitError("simulated partial create failure")

        def remove(self) -> None:
            self.removed = True

    partial = PartialWorktree()
    services = build_services(
        project,
        FakeAgentBackend([]),
        RecordingAdapter(),
        orchestrator_options={
            "qa_worktree_factory": lambda git, path: partial,
        },
    )

    with pytest.raises(GitError, match="partial create"):
        services.orchestrator._with_frozen_candidate(
            "run-partial", 1, services.git.head_sha(), lambda frozen: frozen
        )

    assert partial.removed is True


class RecordingPolicy:
    def __init__(self, delegate: StopPolicy) -> None:
        self.delegate = delegate
        self.elapsed_seconds: list[int] = []

    def evaluate(self, history, **kwargs):
        self.elapsed_seconds.append(kwargs.get("elapsed_seconds", 0))
        return self.delegate.evaluate(history, **kwargs)


def assert_host_violation_audit(
    host_root: Path, project: Path, role: str
) -> dict[str, object]:
    run_dir = next((project / ".hoh" / "runs").iterdir())
    audit = host_root / run_dir.name / "loop-0001" / "audit"
    assert (audit / f"{role}-events-attempt-01.jsonl").is_file()
    receipt_path = audit / "receipts" / f"{role}-attempt-01.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "policy_violation"
    assert receipt["error"]["type"] in {"ProtectedPathError", "RoleOutputError"}
    assert receipt["executable_version"] == "fake-agent/1"
    return receipt


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


def test_product_lock_covers_cleanliness_branch_creation_and_run_shell(
    tmp_path: Path,
) -> None:
    """A second startup must lose at the lock before it can create branch/state."""

    project = initialized_product(tmp_path)
    first = build_services(
        project,
        FakeAgentBackend([]),
        RecordingAdapter(),
        orchestrator_options={"run_id_factory": lambda: "first"},
    )
    second = build_services(
        project,
        FakeAgentBackend([]),
        RecordingAdapter(),
        orchestrator_options={"run_id_factory": lambda: "second"},
    )
    entered = threading.Event()
    release = threading.Event()
    first_errors: list[BaseException] = []

    def hold_locked_drive(run_dir: Path, run_state: dict[str, object]):
        assert (project / ".hoh" / "lock").is_file()
        assert run_dir.name == "first"
        assert run_state["branch"] == "hoh/run-first"
        entered.set()
        assert release.wait(10)
        return dict(run_state)

    first.orchestrator._drive_locked = hold_locked_drive  # type: ignore[method-assign]

    def start_first() -> None:
        try:
            first.orchestrator.run(max_loops=1)
        except BaseException as error:  # pragma: no cover - asserted below
            first_errors.append(error)

    thread = threading.Thread(target=start_first)
    thread.start()
    assert entered.wait(10)
    try:
        with pytest.raises(RunLockedError):
            second.orchestrator.run(max_loops=1)
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert first_errors == []
    branches = run_git(project, "branch", "--format=%(refname:short)").splitlines()
    assert sorted(branches) == ["hoh/run-first", "main"]
    run_dirs = tuple((project / ".hoh" / "runs").iterdir())
    assert [path.name for path in run_dirs] == ["first"]
    state = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert state["branch"] == run_git(project, "branch", "--show-current")


@pytest.mark.parametrize(
    "terminal_status", ("complete", "budget_exhausted", "blocked", "cancelled")
)
def test_new_run_gets_past_product_cleanliness_after_terminal_host_state(
    tmp_path: Path, terminal_status: str
) -> None:
    project = initialized_product(tmp_path)
    prior = project / ".hoh" / "runs" / "prior"
    prior.mkdir(parents=True)
    (prior / "run.json").write_text(
        json.dumps({"status": terminal_status}) + "\n", encoding="utf-8"
    )
    (prior / "run-summary.md").write_text(
        f"Status: {terminal_status}\n", encoding="utf-8"
    )
    (project / ".hoh" / "issue-ledger.json").write_text(
        '{"schema_version": 1, "issues": [], "summary": {}}\n',
        encoding="utf-8",
    )
    services = build_services(
        project,
        FakeAgentBackend([]),
        RecordingAdapter(),
        orchestrator_options={"run_id_factory": lambda: f"next-{terminal_status}"},
    )

    services.orchestrator._drive_locked = (  # type: ignore[method-assign]
        lambda run_dir, run_state: dict(run_state)
    )
    result = services.orchestrator.run(max_loops=1)

    assert result["run_id"] == f"next-{terminal_status}"
    assert result["branch"] == f"hoh/run-next-{terminal_status}"


def test_authoritative_inspection_reconstructs_valid_closed_and_resumable_runs(
    tmp_path: Path,
) -> None:
    project, services = orchestrator_fixture(tmp_path)
    result = services.orchestrator.run(max_loops=1)

    run_dir, status = services.orchestrator.inspect_latest()

    assert run_dir.name == result["run_id"]
    assert status["terminal_status"] == "budget_exhausted"
    assert status["current_candidate"] == result["current_candidate"]
    assert status["completed_loops"] == 1

    resumable_root = tmp_path / "resumable"
    resumable_root.mkdir()
    resumable_project, resumable_services = crashed_after_candidate_fixture(resumable_root)
    resumable_dir, resumable = resumable_services.orchestrator.inspect_latest()
    assert resumable_dir.parent == resumable_project / ".hoh" / "runs"
    assert resumable["terminal_status"] == "resumable"
    assert resumable["current_candidate"] == resumable_services.git.head_sha()
    assert resumable["completed_loops"] == 0


def test_authoritative_inspection_rejects_forged_aggregate_candidate_and_loops(
    tmp_path: Path,
) -> None:
    project, services = orchestrator_fixture(tmp_path)
    result = services.orchestrator.run(max_loops=1)
    run_path = project / ".hoh" / "runs" / str(result["run_id"]) / "run.json"
    state = json.loads(run_path.read_text(encoding="utf-8"))
    forged = "f" * 40
    state.update(
        {
            "status": "complete",
            "reason": "forged complete",
            "current_candidate": forged,
            "best_candidate": forged,
            "loops": [
                {
                    "loop_index": 1,
                    "candidate_sha": forged,
                    "normalized_evidence": {
                        "candidate_sha": forged,
                        "product_complete": True,
                    },
                }
            ],
        }
    )
    run_path.write_text(json.dumps(state) + "\n", encoding="utf-8")

    with pytest.raises(StateConflictError, match="aggregate|candidate|loop|status"):
        services.orchestrator.inspect_latest()


def test_authoritative_inspection_replays_hash_bound_evidence_intents_and_ledger(
    tmp_path: Path,
) -> None:
    project, services = orchestrator_fixture(tmp_path)
    result = services.orchestrator.run(max_loops=1)
    loop_dir = (
        project
        / ".hoh"
        / "runs"
        / str(result["run_id"])
        / "loops"
        / "loop-0001"
    )

    candidate_path = loop_dir / "candidate.json"
    candidate_bytes = candidate_path.read_bytes()
    candidate_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(StateConflictError, match="artifact|candidate"):
        services.orchestrator.inspect_latest()
    candidate_path.write_bytes(candidate_bytes)

    proof_path = loop_dir / "adapter" / "artifacts" / "proof.txt"
    proof_bytes = proof_path.read_bytes()
    proof_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(
        (EvidenceBindingError, StateConflictError), match="artifact|evidence|hash"
    ):
        services.orchestrator.inspect_latest()
    proof_path.write_bytes(proof_bytes)

    ledger_path = project / ".hoh" / "issue-ledger.json"
    ledger_bytes = ledger_path.read_bytes()
    ledger = json.loads(ledger_bytes)
    ledger["applications"][0]["evidence_sha256"] = "0" * 64
    ledger_path.write_text(json.dumps(ledger) + "\n", encoding="utf-8")
    with pytest.raises((StateConflictError, ValueError), match="ledger|evidence"):
        services.orchestrator.inspect_latest()
    ledger_path.write_bytes(ledger_bytes)

    intent_path = loop_dir / "evidence-commit-intent.json"
    intent_bytes = intent_path.read_bytes()
    intent = json.loads(intent_bytes)
    intent["prepared_sha"] = "f" * 40
    intent_path.write_text(json.dumps(intent) + "\n", encoding="utf-8")
    with pytest.raises(StateConflictError, match="commit|intent|Git"):
        services.orchestrator.inspect_latest()
    intent_path.write_bytes(intent_bytes)

    assert services.orchestrator.inspect_latest()[1]["completed_loops"] == 1


def test_authoritative_inspection_rejects_issue_mutation_after_qa_before_closure(
    tmp_path: Path,
) -> None:
    """Trusting mutable issue state when QA evidence is durable must make this fail."""

    project, services = orchestrator_fixture(tmp_path)

    def crash_before_evidence_commit(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("crash after QA before closure")

    services.git.prepare_evidence = crash_before_evidence_commit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after QA before closure"):
        services.orchestrator.run(max_loops=1)

    ledger_path = project / ".hoh" / "issue-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["issues"]) == 1
    assert ledger["issues"][0]["status"] == "open"
    ledger["issues"][0]["status"] = "closed"
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    tampered = ledger_path.read_bytes()

    with pytest.raises(IssueLedgerError, match="durable evidence replay"):
        services.orchestrator.inspect_latest()

    assert ledger_path.read_bytes() == tampered


@pytest.mark.parametrize("cancellation", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_user_cancellation_is_durably_terminal_and_never_resumable(
    tmp_path: Path, cancellation: BaseException
) -> None:
    project = initialized_product(tmp_path)
    backend = ScriptedBackend([cancellation])
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=1)

    assert result["status"] == "cancelled"
    assert result["terminal_status"] == "cancelled"
    assert result["reason"] == "run cancelled by user"
    run_dir = project / ".hoh" / "runs" / str(result["run_id"])
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "cancelled"
    assert state["reason"] == "run cancelled by user"
    assert "resumable" not in json.dumps(state)
    assert "Status: cancelled" in (run_dir / "run-summary.md").read_text(
        encoding="utf-8"
    )
    receipt = next((run_dir / "loops" / "loop-0001" / "receipts").glob("*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["outcome"] == "error"
    assert not (project / ".hoh" / "lock").exists()
    _, inspected = services.orchestrator.inspect_latest()
    assert inspected["status"] == "cancelled"
    assert inspected["reason"] == "run cancelled by user"
    with pytest.raises(ResumeError, match="no resumable"):
        services.orchestrator.resume()


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


def test_developer_protected_path_violation_retains_external_event_and_receipt(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    host_root = tmp_path / "trusted-host-audit"

    def violate_protected_path(request) -> None:
        developer_change(request)
        (request.workspace / "raw-agent-response.json").write_text(
            '{"untrusted": true}\n', encoding="utf-8"
        )
        (request.workspace / ".hoh" / "prd.md").write_text(
            "untrusted host-state mutation\n", encoding="utf-8"
        )

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=violate_protected_path),
        ]
    )
    services = build_services(
        project,
        backend,
        RecordingAdapter(),
        orchestrator_options={"host_staging_root": host_root},
    )

    with pytest.raises(ProtectedPathError, match=r"\.hoh"):
        services.orchestrator.run(max_loops=1)

    receipt = assert_host_violation_audit(host_root, project, "developer")
    assert receipt["role"] == "developer"
    assert "raw-agent-response.json" not in run_git(
        project, "ls-tree", "-r", "--name-only", "HEAD"
    ).splitlines()


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
    _, inspected = services.orchestrator.inspect_latest()
    assert inspected["completed_loops"] == 2
    assert inspected["status"] == "budget_exhausted"


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
    _, inspected = services.orchestrator.inspect_latest()
    assert inspected["status"] == "complete"
    assert inspected["guidance"] == f"git merge {result['current_candidate']}"
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


def test_planner_read_only_violation_retains_external_event_and_receipt(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    host_root = tmp_path / "trusted-host-audit"

    def planner_write(request) -> None:
        developer_change(request, "planner-write\n")
        (request.workspace / "raw-agent-response.json").write_text(
            '{"untrusted": true}\n', encoding="utf-8"
        )

    backend = FakeAgentBackend([FakeResponse(plan(), on_run=planner_write)])
    services = build_services(
        project,
        backend,
        RecordingAdapter(),
        orchestrator_options={"host_staging_root": host_root},
    )

    with pytest.raises(RoleOutputError, match="read-only"):
        services.orchestrator.run(max_loops=1)

    receipt = assert_host_violation_audit(host_root, project, "planner")
    assert receipt["role"] == "planner"
    assert "raw-agent-response.json" not in run_git(
        project, "ls-tree", "-r", "--name-only", "HEAD"
    ).splitlines()
    run_path = next((project / ".hoh" / "runs").glob("*/run.json"))
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state["status"] = "running"
    run_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(StateConflictError, match="receipt|outcome|invocation"):
        services.orchestrator.resume()

    assert len(backend.requests) == 1


def test_qa_read_only_violation_retains_external_event_and_receipt(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    host_root = tmp_path / "trusted-host-audit"

    def qa_write(request) -> None:
        (request.workspace / "product.txt").write_text("qa-write\n", encoding="utf-8")
        (request.workspace / "raw-agent-response.json").write_text(
            '{"untrusted": true}\n', encoding="utf-8"
        )

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_response, on_run=qa_write),
        ]
    )
    services = build_services(
        project,
        backend,
        RecordingAdapter(),
        orchestrator_options={"host_staging_root": host_root},
    )

    with pytest.raises(RoleOutputError, match="read-only"):
        services.orchestrator.run(max_loops=1)

    receipt = assert_host_violation_audit(host_root, project, "qa")
    assert receipt["role"] == "qa"
    assert not any((project / ".hoh" / "tmp").glob("qa-*"))
    assert "raw-agent-response.json" not in run_git(
        project, "ls-tree", "-r", "--name-only", "HEAD"
    ).splitlines()


def test_elapsed_time_is_not_double_counted_across_continuous_loops(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)
    clock = MutableClock()
    config = config_for(project)
    policy = RecordingPolicy(StopPolicy(config))

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

    backend = FakeAgentBackend(
        [
            FakeResponse(plan(1)),
            FakeResponse(developer_response("loop one"), on_run=change_for(1)),
            FakeResponse(qa_for(1)),
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

    result = services.orchestrator.run(max_loops=2)

    assert result["loops_completed"] == 2
    assert policy.elapsed_seconds == [60, 120]
    run_state = json.loads(
        (
            project / ".hoh" / "runs" / result["run_id"] / "run.json"
        ).read_text(encoding="utf-8")
    )
    assert run_state["elapsed_seconds"] == 120


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


def test_evidence_commit_ignores_raw_jsonl_and_named_lookalike_scratch(
    tmp_path: Path,
) -> None:
    project = initialized_product(tmp_path)

    def qa_with_product_scratch(request):
        loop_dir = next(
            (project / ".hoh" / "runs").glob("*/loops/loop-0001")
        )
        (loop_dir / "raw-agent-output.jsonl").write_text(
            '{"untrusted": true}\n', encoding="utf-8"
        )
        lookalike = loop_dir / "responses" / "evidence.json"
        lookalike.parent.mkdir(parents=True)
        lookalike.write_text('{"lookalike": true}\n', encoding="utf-8")
        return qa_response(request)

    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(developer_response(), on_run=developer_change),
            FakeResponse(qa_with_product_scratch),
        ]
    )
    services = build_services(project, backend, RecordingAdapter())

    result = services.orchestrator.run(max_loops=1)

    loop_prefix = f".hoh/runs/{result['run_id']}/loops/loop-0001"
    committed = run_git(project, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert f"{loop_prefix}/raw-agent-output.jsonl" not in committed
    assert f"{loop_prefix}/responses/evidence.json" not in committed
    assert (project / loop_prefix / "raw-agent-output.jsonl").is_file()
    assert (project / loop_prefix / "responses" / "evidence.json").is_file()


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
