from __future__ import annotations

from pathlib import Path

from hoh.policy import StopDecision
from hoh.reporting import build_status, render_run_summary, write_run_summary


def report_fixture() -> dict[str, object]:
    return {
        "run_id": "run-42",
        "start_sha": "start000",
        "current_candidate": "def456",
        "best_candidate": "abc123",
        "elapsed_seconds": 125,
        "loops": [
            {
                "loop_index": 2,
                "normalized_evidence": {
                    "verified_records": [
                        {"claim_id": "zebra"},
                        {"claim_id": "alpha"},
                    ],
                    "diagnostics": [
                        {"category": "protocol", "code": "invalid-evidence"},
                    ],
                },
            },
        ],
        "skill_receipts": [
            {"skill_id": "qa", "sha256": "b" * 64},
            {"skill_id": "core", "sha256": "a" * 64},
        ],
    }


def receipts() -> list[dict[str, object]]:
    return [
        {
            "role": "developer",
            "usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 2_000,
                "reasoning_output_tokens": 500,
            },
        },
        {
            "role": "qa",
            "usage": {
                "input_tokens": 500,
                "cached_input_tokens": 100,
                "output_tokens": 200,
                "reasoning_output_tokens": 0,
            },
        },
    ]


def issue_ledger() -> dict[str, object]:
    return {
        "summary": {"total": 0, "open": 0, "closed": 0, "regressed": 0},
        "issues": [
            {"claim_id": "z-gap", "status": "open", "severity": "major"},
            {"claim_id": "a-gap", "status": "regressed", "severity": "blocker"},
            {"claim_id": "closed", "status": "closed", "severity": "minor"},
        ],
    }


def test_report_contains_candidate_cost_and_resume(tmp_path: Path) -> None:
    status = build_status(
        report_fixture(),
        receipts=receipts(),
        decision=StopDecision(True, "blocked", "unrecoverable protocol failure"),
        issue_ledger=issue_ledger(),
    )

    rendered = render_run_summary(status)

    assert "Status: blocked" in rendered
    assert "Best candidate: abc123" in rendered
    assert "Total tokens: 4200" in rendered
    assert "Remaining gaps" in rendered
    assert "hoh resume --run-id run-42" in rendered
    assert write_run_summary(tmp_path, rendered) == tmp_path / "run-summary.md"
    assert (tmp_path / "run-summary.md").read_text(encoding="utf-8") == rendered


def test_status_is_derived_from_receipts_and_ledger_in_stable_order() -> None:
    state = report_fixture()
    state["total_tokens"] = 99_999
    state["best_candidate"] = "a" * 40
    status = build_status(
        state,
        receipts=receipts(),
        decision=StopDecision(True, "complete", "all required claims verified"),
        issue_ledger=issue_ledger(),
    )

    assert status["completed_loops"] == 1
    assert status["role_attempts"] == {"developer": 1, "qa": 1}
    assert status["total_tokens"] == 4_200
    assert status["verified_claim_ids"] == ["alpha", "zebra"]
    assert [gap["claim_id"] for gap in status["remaining_gaps"]] == ["a-gap", "z-gap"]
    assert status["issue_summary"] == {
        "total": 3,
        "open": 1,
        "closed": 1,
        "regressed": 1,
    }
    assert status["guidance"] == "git merge " + "a" * 40


def test_status_accepts_persisted_skill_records_and_renders_failure_details() -> None:
    state = report_fixture()
    state["skills"] = state.pop("skill_receipts")
    status = build_status(
        state,
        decision=StopDecision(True, "blocked", "unrecoverable protocol failure"),
    )

    rendered = render_run_summary(status)

    assert status["skills"] == [
        {"skill_id": "core", "sha256": "a" * 64},
        {"skill_id": "qa", "sha256": "b" * 64},
    ]
    assert status["failure_category"] == "protocol"
    assert "- protocol: invalid-evidence" in rendered
    assert "- core: " + "a" * 64 in rendered


def test_invalid_run_ids_never_produce_resume_commands_or_markdown_lines() -> None:
    for run_id in ("run&Write-Output injected", "run-42\r\nStatus: complete"):
        state = report_fixture()
        state["run_id"] = run_id

        status = build_status(
            state,
            decision=StopDecision(True, "blocked", "unrecoverable protocol failure"),
        )

        rendered = render_run_summary(status)

        assert status["guidance"].startswith("Manual action required:")
        assert "hoh resume" not in status["guidance"]
        assert "\r" not in rendered
        assert "\nStatus: complete" not in rendered


def test_terminal_failure_category_comes_from_decision_not_old_diagnostics() -> None:
    state = report_fixture()
    state["diagnostics"] = [{"category": "infrastructure", "code": "old-network"}]

    status = build_status(
        state,
        decision=StopDecision(True, "blocked", "unrecoverable protocol failure: bad-evidence"),
    )

    assert status["failure_category"] == "protocol"


def test_complete_without_best_candidate_merges_a_bound_current_candidate() -> None:
    candidate = "c" * 40
    state = report_fixture()
    state.pop("best_candidate")
    state["current_candidate"] = candidate
    state["loops"] = [
        {
            "normalized_evidence": {
                "candidate_sha": candidate,
                "product_complete": True,
            },
        }
    ]

    status = build_status(
        state,
        decision=StopDecision(True, "complete", "all required claims verified"),
    )

    assert status["best_candidate"] == candidate
    assert status["guidance"] == f"git merge {candidate}"


def test_malformed_complete_state_requires_manual_action_instead_of_resume() -> None:
    state = report_fixture()
    state.pop("best_candidate")
    state["current_candidate"] = "not-a-full-sha"

    status = build_status(
        state,
        decision=StopDecision(True, "complete", "all required claims verified"),
    )

    assert status["guidance"].startswith("Manual action required:")
    assert "hoh resume" not in status["guidance"]


def test_complete_with_short_best_candidate_requires_manual_action() -> None:
    state = report_fixture()
    state["best_candidate"] = "abc123"

    status = build_status(
        state,
        decision=StopDecision(True, "complete", "all required claims verified"),
    )

    assert status["guidance"].startswith("Manual action required:")
    assert "git merge" not in status["guidance"]
