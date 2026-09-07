from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from hoh.state.issue_ledger import IssueLedger, IssueLedgerError


MISSING = object()


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


def write_legacy_open_issue(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issues": [
                    {
                        "claim_id": "player-moves",
                        "status": "open",
                        "severity": "major",
                        "impact": "player-moves is unavailable",
                        "recommended_update": "implement player-moves",
                        "validation_requirement": "recheck player-moves",
                        "history": [
                            {
                                "loop": 1,
                                "candidate": "a" * 40,
                                "evidence_path": None,
                                "observation": "could not verify player-moves",
                                "status": "open",
                            }
                        ],
                    }
                ],
                "summary": {"total": 1, "open": 1, "closed": 0, "regressed": 0},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def downgrade_native_ledger_to_schema_v1(path: Path) -> None:
    """Model the persisted shape emitted before correlation metadata existed."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 1
    document.pop("application_correlation_start_loop", None)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


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


def test_exact_replay_rejects_duplicate_persisted_claim_ids_without_writing(
    tmp_path: Path,
) -> None:
    """Returning before duplicate issue validation must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 1)
    document = ledger.load()
    document["issues"].append(deepcopy(document["issues"][0]))
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="duplicate issue claim_id"):
        ledger.apply(replayed, 1)

    assert path.read_bytes() == tampered


@pytest.mark.parametrize("malformed_history", ["not-a-list", [{"loop": 0}]])
def test_exact_replay_rejects_malformed_persisted_history_without_writing(
    tmp_path: Path, malformed_history: object
) -> None:
    """Returning before complete history validation must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 1)
    document = ledger.load()
    document["issues"][0]["history"] = malformed_history
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="history"):
        ledger.apply(replayed, 1)

    assert path.read_bytes() == tampered


@pytest.mark.parametrize(
    ("field", "corrupt_value"),
    [
        ("candidate", MISSING),
        ("candidate", "not-a-sha"),
        ("candidate", "b" * 40),
        ("loop", 2),
        ("evidence_path", MISSING),
        ("evidence_path", "unexpected-for-gap.log"),
        ("observation", MISSING),
        ("observation", 7),
        ("status", MISSING),
        ("status", "unknown"),
    ],
    ids=[
        "missing-candidate",
        "corrupt-candidate",
        "candidate-does-not-match-application",
        "loop-has-no-application",
        "missing-evidence-path",
        "gap-path-not-null",
        "missing-observation",
        "corrupt-observation",
        "missing-status",
        "corrupt-status",
    ],
)
def test_exact_replay_rejects_missing_or_corrupt_history_provenance_without_writing(
    tmp_path: Path, field: str, corrupt_value: object
) -> None:
    """Accepting incomplete persisted provenance before replay must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 1)
    document = ledger.load()
    event = document["issues"][0]["history"][0]
    if corrupt_value is MISSING:
        event.pop(field)
    else:
        event[field] = corrupt_value
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="history"):
        ledger.apply(replayed, 1)

    assert path.read_bytes() == tampered


def test_exact_replay_rejects_closed_history_without_evidence_path(
    tmp_path: Path,
) -> None:
    """Allowing a closed event without verification provenance must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    ledger.apply(evidence("a" * 40, gap_records=[gap("player-moves")]), 1)
    replayed = evidence("b" * 40, verified_records=[verified("player-moves")])
    ledger.apply(replayed, 2)
    document = ledger.load()
    document["issues"][0]["history"][-1]["evidence_path"] = None
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="history evidence_path"):
        ledger.apply(replayed, 2)

    assert path.read_bytes() == tampered


def test_exact_replay_rejects_invalid_persisted_status_transition_without_writing(
    tmp_path: Path,
) -> None:
    """Accepting a lifecycle that starts closed must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 1)
    document = ledger.load()
    issue = document["issues"][0]
    issue["status"] = "closed"
    issue["history"][0]["status"] = "closed"
    issue["history"][0]["evidence_path"] = "checks/forged.log"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="history status transition"):
        ledger.apply(replayed, 1)

    assert path.read_bytes() == tampered


@pytest.mark.parametrize(
    ("field", "corrupt_value"),
    [
        ("severity", MISSING),
        ("severity", "critical"),
        ("impact", 7),
        ("recommended_update", MISSING),
        ("validation_requirement", ""),
        ("status", "closed"),
        ("history", []),
    ],
    ids=[
        "missing-severity",
        "corrupt-severity",
        "corrupt-impact",
        "missing-recommended-update",
        "empty-validation-requirement",
        "status-does-not-match-history",
        "empty-history",
    ],
)
def test_exact_replay_rejects_malformed_current_issue_fields_without_writing(
    tmp_path: Path, field: str, corrupt_value: object
) -> None:
    """Returning before current issue validation must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 1)
    document = ledger.load()
    issue = document["issues"][0]
    if corrupt_value is MISSING:
        issue.pop(field)
    else:
        issue[field] = corrupt_value
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="issue"):
        ledger.apply(replayed, 1)

    assert path.read_bytes() == tampered


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


def test_legacy_history_migration_boundary_supports_replay_and_future_loops(
    tmp_path: Path,
) -> None:
    """Requiring applications for pre-migration history must make this fail."""
    path = tmp_path / "issue-ledger.json"
    write_legacy_open_issue(path)
    ledger = IssueLedger(path)
    loop_2 = evidence(
        "b" * 40,
        verified_records=[verified("player-moves", "checks/pass-2.log")],
    )

    ledger.apply(loop_2, 2)
    first_write = path.read_bytes()
    ledger.apply(loop_2, 2)

    migrated = ledger.load()
    assert path.read_bytes() == first_write
    assert migrated["schema_version"] == 2
    assert migrated["application_correlation_start_loop"] == 2
    assert [application["loop"] for application in migrated["applications"]] == [2]
    assert [event["loop"] for event in migrated["issues"][0]["history"]] == [1, 2]

    loop_3 = evidence("c" * 40, gap_records=[gap("player-moves")])
    ledger.apply(loop_3, 3)
    second_write = path.read_bytes()
    ledger.apply(loop_3, 3)

    advanced = ledger.load()
    assert path.read_bytes() == second_write
    assert [application["loop"] for application in advanced["applications"]] == [2, 3]
    assert [event["loop"] for event in advanced["issues"][0]["history"]] == [1, 2, 3]
    assert advanced["issues"][0]["status"] == "regressed"


def test_migrated_ledger_rejects_missing_correlated_applications_without_writing(
    tmp_path: Path,
) -> None:
    """Disabling correlation when applications are removed must make this fail."""
    path = tmp_path / "issue-ledger.json"
    write_legacy_open_issue(path)
    ledger = IssueLedger(path)
    ledger.apply(
        evidence("b" * 40, verified_records=[verified("player-moves")]),
        2,
    )
    document = ledger.load()
    document["schema_version"] = 2
    document["application_correlation_start_loop"] = 2
    document["applications"] = []
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="application"):
        ledger.apply(evidence("c" * 40, gap_records=[gap("player-moves")]), 3)

    assert path.read_bytes() == tampered


def test_migrated_ledger_rejects_missing_future_application_without_writing(
    tmp_path: Path,
) -> None:
    """Blaming pre-migration history instead of a later missing application must fail."""
    path = tmp_path / "issue-ledger.json"
    write_legacy_open_issue(path)
    ledger = IssueLedger(path)
    ledger.apply(
        evidence("b" * 40, verified_records=[verified("player-moves")]),
        2,
    )
    document = ledger.load()
    document["schema_version"] = 2
    document["application_correlation_start_loop"] = 2
    issue = document["issues"][0]
    issue["status"] = "regressed"
    issue["history"].append(
        {
            "loop": 3,
            "candidate": "c" * 40,
            "evidence_path": None,
            "observation": "could not verify player-moves",
            "status": "regressed",
        }
    )
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="loop 3 has no application"):
        ledger.apply(evidence("d" * 40, gap_records=[gap("player-moves")]), 4)

    assert path.read_bytes() == tampered


def test_migrated_ledger_rejects_malformed_boundary_application_without_writing(
    tmp_path: Path,
) -> None:
    """Correlating legacy history instead of the boundary event must make this fail."""
    path = tmp_path / "issue-ledger.json"
    write_legacy_open_issue(path)
    ledger = IssueLedger(path)
    ledger.apply(
        evidence("b" * 40, verified_records=[verified("player-moves")]),
        2,
    )
    document = ledger.load()
    document["schema_version"] = 2
    document["application_correlation_start_loop"] = 2
    document["applications"][0]["candidate"] = "d" * 40
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(
        IssueLedgerError, match="history candidate does not match its application"
    ):
        ledger.apply(evidence("c" * 40, gap_records=[gap("player-moves")]), 3)

    assert path.read_bytes() == tampered


def test_native_ledger_requires_applications_for_all_native_history(
    tmp_path: Path,
) -> None:
    """Treating native history without applications as legacy must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    ledger.apply(evidence("a" * 40, gap_records=[gap("player-moves")]), 1)
    document = ledger.load()
    document["schema_version"] = 2
    document["application_correlation_start_loop"] = 1
    document["applications"] = []
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="application"):
        ledger.apply(
            evidence("b" * 40, verified_records=[verified("player-moves")]),
            2,
        )

    assert path.read_bytes() == tampered


def test_native_ledger_starting_at_later_loop_does_not_create_legacy_history(
    tmp_path: Path,
) -> None:
    """Using the first application as every native boundary must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    loop_2 = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(loop_2, 2)
    document = ledger.load()
    document["issues"][0]["history"].insert(
        0,
        {
            "loop": 1,
            "candidate": "b" * 40,
            "evidence_path": None,
            "observation": "could not verify player-moves",
            "status": "open",
        },
    )
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="loop 1 has no application"):
        ledger.apply(loop_2, 2)

    assert path.read_bytes() == tampered


@pytest.mark.parametrize(
    "persisted_boundary",
    [MISSING, 2],
    ids=["inferred-first-application", "legacy-explicit-boundary"],
)
def test_schema_v1_application_state_rejects_uncorrelated_prefix_without_writing(
    tmp_path: Path, persisted_boundary: object
) -> None:
    """Treating an unauthenticated v1 prefix as migrated history must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 2)
    downgrade_native_ledger_to_schema_v1(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    if persisted_boundary is not MISSING:
        document["application_correlation_start_loop"] = persisted_boundary
    document["issues"][0]["history"].insert(
        0,
        {
            "loop": 1,
            "candidate": "b" * 40,
            "evidence_path": None,
            "observation": "could not verify player-moves",
            "status": "open",
        },
    )
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="loop 1 has no application"):
        ledger.apply(replayed, 2)

    assert path.read_bytes() == tampered


def test_fully_correlated_schema_v1_starting_at_loop_2_upgrades_from_loop_1(
    tmp_path: Path,
) -> None:
    """Inferring loop 2 for fully correlated v1 state must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    loop_2 = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(loop_2, 2)
    downgrade_native_ledger_to_schema_v1(path)
    pre_upgrade = path.read_bytes()

    ledger.apply(loop_2, 2)

    assert path.read_bytes() == pre_upgrade

    ledger.apply(
        evidence("b" * 40, verified_records=[verified("player-moves")]),
        3,
    )

    upgraded = ledger.load()
    assert upgraded["schema_version"] == 2
    assert upgraded["application_correlation_start_loop"] == 1
    assert [application["loop"] for application in upgraded["applications"]] == [
        2,
        3,
    ]
    assert [event["loop"] for event in upgraded["issues"][0]["history"]] == [2, 3]


def test_native_schema_requires_explicit_application_correlation_boundary(
    tmp_path: Path,
) -> None:
    """Accepting a native ledger without its boundary must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    ledger.apply(evidence("a" * 40, gap_records=[gap("player-moves")]), 1)
    document = ledger.load()
    document["schema_version"] = 2
    document.pop("application_correlation_start_loop", None)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="correlation boundary"):
        ledger.apply(
            evidence("b" * 40, verified_records=[verified("player-moves")]),
            2,
        )

    assert path.read_bytes() == tampered


@pytest.mark.parametrize("schema_version", [True, 2.0])
def test_application_correlation_schema_version_must_be_an_integer(
    tmp_path: Path, schema_version: object
) -> None:
    """Letting bools or floats select boundary semantics must make this fail."""
    path = tmp_path / "issue-ledger.json"
    ledger = IssueLedger(path)
    replayed = evidence("a" * 40, gap_records=[gap("player-moves")])
    ledger.apply(replayed, 1)
    document = ledger.load()
    document["schema_version"] = schema_version
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    tampered = path.read_bytes()

    with pytest.raises(IssueLedgerError, match="schema_version"):
        ledger.apply(replayed, 1)

    assert path.read_bytes() == tampered
