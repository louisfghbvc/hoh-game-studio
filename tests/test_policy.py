from pathlib import Path

from hoh.models import HarnessConfig
from hoh.policy import StopDecision, StopPolicy


def policy() -> StopPolicy:
    return StopPolicy(
        HarnessConfig(
            project=Path("."),
            adapter="command",
            model="test-model",
            reasoning_effort="high",
            codex_bin="codex",
            max_loops=12,
            max_priorities_per_loop=3,
            max_role_retries=1,
            max_consecutive_no_progress=3,
            max_consecutive_same_blocker=3,
            role_timeout_minutes=45,
            max_total_tokens=100,
            max_elapsed_minutes=10,
            protected_paths=(".hoh", ".git"),
        )
    )


def loop(
    verified_claim_ids: set[str] = set(),
    *,
    required_claim_ids: set[str] = {"a"},
    product_complete: bool = False,
    gaps: list[dict[str, str]] | None = None,
    issue_summary: dict[str, object] | None = None,
    acceptance_claim_ids: set[str] = set(),
    agent_completed: bool = False,
) -> dict[str, object]:
    candidate_sha = "a" * 40
    qa_invocation_id = "loop-qa-1"
    return {
        "normalized_evidence": {
            "candidate_sha": candidate_sha,
            "product_complete": product_complete,
            "qa_status": "pass",
            "verified_records": [
                {"claim_id": claim_id} for claim_id in sorted(verified_claim_ids)
            ],
            "gap_records": gaps or [],
            "host_metadata": {
                "deterministic_checks_passed": True,
                "required_claim_ids": sorted(required_claim_ids),
                "verified_required_claim_ids": sorted(
                    verified_claim_ids & required_claim_ids
                ),
            },
        },
        "deterministic_checks_passed": True,
        "deterministic_checks_candidate_sha": candidate_sha,
        "qa_invocation_id": qa_invocation_id,
        "issues": [],
        "issues_candidate_sha": candidate_sha,
        "issue_summary": issue_summary
        or {
            "candidate_sha": candidate_sha,
            "severity_complete": True,
            "open_blocker": 0,
            "open_major": 0,
        },
        "release_gate": {
            "invocation_id": "release-qa-1",
            "scope": "full_release",
            "qa_status": "pass",
            "end_to_end_passed": True,
            "deterministic_checks_passed": True,
            "candidate_sha": candidate_sha,
        },
        "acceptance_claim_ids": sorted(acceptance_claim_ids),
        "agent_completed": agent_completed,
    }


def complete_history_at_loop_12() -> list[dict[str, object]]:
    history = [loop({"a"})]
    history[-1]["normalized_evidence"]["product_complete"] = True  # type: ignore[index]
    return history * 12


def gap_loop(claim_id: str) -> dict[str, object]:
    return loop(gaps=[{"claim_id": claim_id, "severity": "blocker"}])


def verified_loop(claim_ids: set[str]) -> dict[str, object]:
    return loop(claim_ids)


def test_verified_completion_wins_before_budget_stop() -> None:
    decision = policy().evaluate(
        history=complete_history_at_loop_12(), total_tokens=100, elapsed_seconds=600
    )

    assert decision == StopDecision(True, "complete", "all required claims verified")


def test_same_blocker_three_times_stops() -> None:
    history = [gap_loop("build:blocker") for _ in range(3)]

    assert policy().evaluate(history).terminal_status == "blocked"


def test_two_no_progress_loops_continue() -> None:
    history = [verified_loop({"a"}), verified_loop({"a"})]

    assert policy().evaluate(history).should_stop is False


def test_three_no_progress_loops_stop() -> None:
    history = [verified_loop({"a"}) for _ in range(4)]

    assert policy().evaluate(history) == StopDecision(
        True, "blocked", "no measurable evidence progress for 3 consecutive loops"
    )


def test_new_verified_required_claim_is_measurable_progress() -> None:
    history = [
        loop({"a"}, required_claim_ids={"a", "b"}),
        loop({"a", "b"}, required_claim_ids={"a", "b"}),
        loop({"a", "b"}, required_claim_ids={"a", "b"}),
    ]

    snapshot = policy().progress(history)

    assert snapshot.made_progress is False
    assert snapshot.consecutive_no_progress == 1
    assert snapshot.verified_required_claim_ids == frozenset({"a", "b"})


def test_reclosed_existing_issue_is_measurable_progress() -> None:
    history = [
        {**loop(), "issues": [{"claim_id": "save", "status": "open", "severity": "major"}]},
        {**loop(), "issues": [{"claim_id": "save", "status": "closed", "severity": "major"}]},
        {**loop(), "issues": [{"claim_id": "save", "status": "regressed", "severity": "major"}]},
        {**loop(), "issues": [{"claim_id": "save", "status": "closed", "severity": "major"}]},
    ]

    snapshot = policy().progress(history)

    assert snapshot.made_progress is True
    assert snapshot.consecutive_no_progress == 0


def test_new_file_or_agent_completion_claim_is_not_progress() -> None:
    history = [
        loop({"a"}),
        {**loop({"a"}, agent_completed=True), "changed_paths": ["new-file.py"]},
        {**loop({"a"}, agent_completed=True), "changed_paths": ["another-file.py"]},
        {**loop({"a"}, agent_completed=True), "changed_paths": ["final-file.py"]},
    ]

    assert policy().evaluate(history).should_stop is True


def test_explicit_cancellation_precedes_unrecoverable_failure() -> None:
    decision = policy().evaluate(
        [loop({"a"})],
        cancelled=True,
        unrecoverable_failure="infrastructure:codex-unavailable",
    )

    assert decision == StopDecision(True, "cancelled", "run cancelled by user")


def test_unrecoverable_protocol_failure_precedes_repeated_blocker() -> None:
    decision = policy().evaluate(
        [gap_loop("build:blocker") for _ in range(3)],
        unrecoverable_failure="protocol:invalid-evidence",
    )

    assert decision == StopDecision(
        True, "blocked", "unrecoverable protocol failure: invalid-evidence"
    )


def test_token_elapsed_and_loop_budgets_stop_in_order() -> None:
    decision = policy().evaluate(
        [
            loop({f"claim-{index}"}, required_claim_ids={f"claim-{index}"})
            for index in range(12)
        ],
        total_tokens=100,
        elapsed_seconds=600,
    )

    assert decision == StopDecision(True, "budget_exhausted", "token budget exhausted")


def test_completion_requires_host_evidence_not_agent_claim() -> None:
    decision = policy().evaluate([loop({"a"}, agent_completed=True)])

    assert decision.should_stop is False


def test_completion_requires_passing_qa_status() -> None:
    history = complete_history_at_loop_12()
    history[-1]["normalized_evidence"]["qa_status"] = "fail"  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_requires_a_fresh_host_release_gate() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"] = {}

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_aggregate_only_issue_summary() -> None:
    history = complete_history_at_loop_12()
    history[-1].pop("issues")
    history[-1]["issue_summary"] = {"total": 3, "open": 0, "closed": 3, "regressed": 0}

    assert policy().evaluate(history).terminal_status != "complete"


def test_explicit_empty_latest_evidence_cannot_borrow_history_state() -> None:
    decision = policy().evaluate(complete_history_at_loop_12(), latest_evidence={})

    assert decision.terminal_status != "complete"


def test_completion_rejects_a_release_gate_for_another_candidate() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"]["candidate_sha"] = "b" * 40  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def test_unknown_explicit_failure_category_does_not_stop_the_run() -> None:
    decision = policy().evaluate([loop()], unrecoverable_failure="candidate:build-failed")

    assert decision.should_stop is False


def test_aggregate_closed_issue_counts_do_not_create_progress() -> None:
    history = [
        loop(issue_summary={"closed": closed})
        for closed in range(1, 4)
    ]

    assert policy().evaluate(history) == StopDecision(
        True, "blocked", "no measurable evidence progress for 3 consecutive loops"
    )


def test_completion_rejects_a_release_gate_using_the_loop_qa_invocation() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"]["invocation_id"] = "loop-qa-1"  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_requires_release_e2e_success() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"].pop("end_to_end_passed")  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def valid_closed_issue(claim_id: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "status": "closed",
        "severity": "minor",
        "impact": "Player-visible save state.",
        "recommended_update": "Keep the verified behavior.",
        "validation_requirement": "Run the save check.",
        "history": [
            {
                "loop": 1,
                "candidate": "b" * 40,
                "evidence_path": None,
                "observation": "save behavior needs verification",
                "status": "open",
            },
            {
                "loop": 2,
                "candidate": "b" * 40,
                "evidence_path": "checks/save.log",
                "observation": "save behavior passed",
                "status": "closed",
            }
        ],
    }


def valid_issue(claim_id: str, status: str, severity: str) -> dict[str, object]:
    issue = valid_closed_issue(claim_id)
    issue["status"] = status
    issue["severity"] = severity
    history = issue["history"]
    assert isinstance(history, list)
    if status == "open":
        issue["history"] = [history[0]]
    elif status == "regressed":
        history.append(
            {
                "loop": 3,
                "candidate": "b" * 40,
                "evidence_path": None,
                "observation": "save behavior regressed",
                "status": "regressed",
            }
        )
    return issue


def test_completion_rejects_issue_without_auditable_history() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [
        {"claim_id": "save", "status": "closed", "severity": "minor"}
    ]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_duplicate_authoritative_issue_ids() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_closed_issue("save"), valid_closed_issue("save")]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_requires_an_explicit_check_candidate_sha() -> None:
    history = complete_history_at_loop_12()
    history[-1].pop("deterministic_checks_candidate_sha")

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_requires_an_explicit_issue_candidate_sha() -> None:
    history = complete_history_at_loop_12()
    history[-1].pop("issues_candidate_sha")

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_mismatched_check_candidate_sha() -> None:
    history = complete_history_at_loop_12()
    history[-1]["deterministic_checks_candidate_sha"] = "b" * 40

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_mismatched_issue_candidate_sha() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues_candidate_sha"] = "b" * 40

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_accepts_a_strictly_valid_closed_issue() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_closed_issue("save")]

    assert policy().evaluate(history).terminal_status == "complete"


def test_completion_rejects_a_canonical_open_blocker_issue() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_issue("save", "open", "blocker")]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_canonical_open_major_issue() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_issue("save", "open", "major")]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_canonical_regressed_blocker_issue() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_issue("save", "regressed", "blocker")]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_canonical_regressed_major_issue() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_issue("save", "regressed", "major")]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_allows_a_canonical_open_minor_issue() -> None:
    history = complete_history_at_loop_12()
    history[-1]["issues"] = [valid_issue("save", "open", "minor")]

    assert policy().evaluate(history).terminal_status == "complete"


def test_completion_rejects_a_release_gate_with_wrong_scope() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"]["scope"] = "smoke"  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_release_gate_with_qa_failure() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"]["qa_status"] = "fail"  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_rejects_a_release_gate_with_failed_deterministic_checks() -> None:
    history = complete_history_at_loop_12()
    history[-1]["release_gate"]["deterministic_checks_passed"] = False  # type: ignore[index]

    assert policy().evaluate(history).terminal_status != "complete"


def test_completion_requires_an_ordinary_qa_invocation_id() -> None:
    history = complete_history_at_loop_12()
    history[-1].pop("qa_invocation_id")

    assert policy().evaluate(history).terminal_status != "complete"
