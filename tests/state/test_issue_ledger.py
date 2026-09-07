from __future__ import annotations

import json
from pathlib import Path

import pytest

from hoh.state.issue_ledger import IssueLedger, IssueLedgerError


def evidence(
    candidate: str,
    *,
    verified_records: list[dict[str, object]] | None = None,
    gap_records: list[dict[str, object]] | None = None,
    diagnostics: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "candidate_sha": candidate,
        "verified_records": verified_records or [],
        "gap_records": gap_records or [],
        "diagnostics": diagnostics or [],
    }


def verified(claim_id: str, path: str = "checks/player.log") -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "claim": f"{claim_id} works",
        "observations": [f"observed {claim_id}"],
        "execution_records": [
            {
                "type": "log",
                "path": path,
                "sha256": "a" * 64,
                "observation": f"recorded {claim_id}",
            }
        ],
        "preservation_requirement": f"preserve {claim_id}",
    }


def gap(claim_id: str, severity: str = "major") -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "severity": severity,
        "impact": f"{claim_id} is unavailable",
        "observations": [f"could not verify {claim_id}"],
        "recommended_update": f"implement {claim_id}",
        "validation_requirement": f"recheck {claim_id}",
    }


def test_verified_claim_that_later_gaps_becomes_regressed(tmp_path: Path) -> None:
    """Treating a reopened verified issue as merely open must make this fail."""
    ledger = IssueLedger(tmp_path / "issue-ledger.json")
    ledger.apply(evidence("a" * 40, gap_records=[gap("player-moves")]), 1)
    ledger.apply(evidence("b" * 40, verified_records=[verified("player-moves")]), 2)
    ledger.apply(evidence("c" * 40, gap_records=[gap("player-moves")]), 3)

    issue = ledger.load()["issues"][0]

    assert issue["status"] == "regressed"
    assert [event["status"] for event in issue["history"]] == [
        "open",
        "closed",
        "regressed",
    ]


def test_regressed_claim_can_close_again_and_retains_auditable_history(
    tmp_path: Path,
) -> None:
    """Dropping a lifecycle event or its provenance must make this fail."""
    ledger = IssueLedger(tmp_path / "issue-ledger.json")
    ledger.apply(evidence("a" * 40, gap_records=[gap("player-moves")]), 1)
    ledger.apply(
        evidence(
            "b" * 40,
            verified_records=[verified("player-moves", "checks/pass-2.log")],
        ),
        2,
    )
    ledger.apply(evidence("c" * 40, gap_records=[gap("player-moves")]), 3)
    ledger.apply(
        evidence(
            "d" * 40,
            verified_records=[verified("player-moves", "checks/pass-4.log")],
        ),
        4,
    )

    issue = ledger.load()["issues"][0]

    assert issue["status"] == "closed"
    assert issue["history"] == [
        {
            "loop": 1,
            "candidate": "a" * 40,
            "evidence_path": None,
            "observation": "could not verify player-moves",
            "status": "open",
        },
        {
            "loop": 2,
            "candidate": "b" * 40,
            "evidence_path": "checks/pass-2.log",
            "observation": "recorded player-moves",
            "status": "closed",
        },
        {
            "loop": 3,
            "candidate": "c" * 40,
            "evidence_path": None,
            "observation": "could not verify player-moves",
            "status": "regressed",
        },
        {
            "loop": 4,
            "candidate": "d" * 40,
            "evidence_path": "checks/pass-4.log",
            "observation": "recorded player-moves",
            "status": "closed",
        },
    ]


def test_summary_is_derived_from_sorted_issue_entries_on_every_write(tmp_path: Path) -> None:
    """Trusting stored or agent-supplied counts must make this fail."""
    path = tmp_path / "issue-ledger.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issues": [],
                "summary": {"total": 99, "open": 99, "closed": 99, "regressed": 99},
            }
        ),
        encoding="utf-8",
    )
    ledger = IssueLedger(path)

    ledger.apply(
        evidence(
            "a" * 40,
            gap_records=[gap("z-last"), gap("a-first", "blocker")],
        ),
        1,
    )
    ledger.apply(evidence("b" * 40, verified_records=[verified("a-first")]), 2)

    document = ledger.load()

    assert [issue["claim_id"] for issue in document["issues"]] == ["a-first", "z-last"]
    assert document["summary"] == {
        "total": 2,
        "open": 1,
        "closed": 1,
        "regressed": 0,
    }
    assert ledger.summary() == document["summary"]


def test_load_replaces_forged_summary_with_counts_derived_from_issues(
    tmp_path: Path,
) -> None:
    """Returning a persisted summary without recounting issues must make this fail."""
    path = tmp_path / "issue-ledger.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issues": [
                    {
                        "claim_id": "player-moves",
                        "status": "open",
                        "history": [],
                    }
                ],
                "summary": {"total": 0, "open": 0, "closed": 0, "regressed": 0},
            }
        ),
        encoding="utf-8",
    )

    loaded = IssueLedger(path).load()

    assert loaded["summary"] == {
        "total": 1,
        "open": 1,
        "closed": 0,
        "regressed": 0,
    }


def test_infrastructure_and_protocol_diagnostics_do_not_become_product_issues(
    tmp_path: Path,
) -> None:
    """Conflating host failures with product gaps must make this fail."""
    ledger = IssueLedger(tmp_path / "issue-ledger.json")
    diagnostics = [
        {"category": "infrastructure", "code": "runner-offline", "message": "offline"},
        {"category": "protocol", "code": "malformed-json", "message": "invalid"},
    ]

    ledger.apply(evidence("a" * 40, diagnostics=diagnostics), 1)

    assert ledger.load()["issues"] == []
    assert ledger.summary() == {"total": 0, "open": 0, "closed": 0, "regressed": 0}


def test_exact_evidence_replay_is_idempotent_and_uses_canonical_identity(
    tmp_path: Path,
) -> None:
    """Appending history for an exact semantic replay must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    original = evidence("a" * 40, gap_records=[gap("player-moves")])
    reordered = dict(reversed(list(original.items())))

    ledger.apply(original, 1)
    first_write = path.read_bytes()
    ledger.apply(reordered, 1)

    document = ledger.load()
    assert path.read_bytes() == first_write
    assert len(document["issues"][0]["history"]) == 1
    assert len(document["applications"]) == 1
    application = document["applications"][0]
    assert set(application) == {"loop", "candidate", "evidence_sha256"}
    assert application["loop"] == 1
    assert application["candidate"] == "a" * 40
    assert len(application["evidence_sha256"]) == 64


@pytest.mark.parametrize("conflict", ["candidate", "evidence"])
def test_same_loop_with_different_application_identity_is_rejected(
    tmp_path: Path, conflict: str
) -> None:
    """Accepting two identities for one loop must make this fail."""
    ledger = IssueLedger(tmp_path / "issue-ledger.json")
    ledger.apply(evidence("a" * 40, gap_records=[gap("player-moves")]), 1)
    candidate = "b" * 40 if conflict == "candidate" else "a" * 40
    severity = "major" if conflict == "candidate" else "blocker"

    with pytest.raises(IssueLedgerError, match="same loop"):
        ledger.apply(
            evidence(candidate, gap_records=[gap("player-moves", severity)]),
            1,
        )


def test_older_loop_after_newer_application_is_rejected(tmp_path: Path) -> None:
    """Allowing out-of-order applications to regress state must make this fail."""
    ledger = IssueLedger(tmp_path / "issue-ledger.json")
    ledger.apply(evidence("b" * 40, gap_records=[gap("player-moves")]), 2)

    with pytest.raises(IssueLedgerError, match="out of order"):
        ledger.apply(
            evidence("a" * 40, verified_records=[verified("player-moves")]),
            1,
        )

    issue = ledger.load()["issues"][0]
    assert issue["status"] == "open"
    assert [event["loop"] for event in issue["history"]] == [2]
