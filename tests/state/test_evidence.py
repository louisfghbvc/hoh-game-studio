from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from hoh.state.evidence import EvidenceBindingError, EvidenceNormalizer


FRAME_SHA256 = "9dff50df08c635815f4b19da10f756605a34a79a48d4ba48712782502975a70e"
EXPECTED_CANDIDATE = "a" * 40
EXPECTED_TREE = "b" * 64


def requirements(*, required: tuple[str, ...] = ("player-moves",)) -> dict[str, object]:
    claim_ids = ("player-moves", "optional-polish")
    return {
        "schema_version": 1,
        "claims": [
            {
                "id": claim_id,
                "description": claim_id.replace("-", " "),
                "required": claim_id in required,
            }
            for claim_id in claim_ids
        ],
    }


def evidence(
    *,
    candidate_sha: str = EXPECTED_CANDIDATE,
    artifact_tree_sha256: str = EXPECTED_TREE,
    verified_records: list[dict[str, object]] | None = None,
    gap_records: list[dict[str, object]] | None = None,
    product_complete: bool = True,
) -> dict[str, object]:
    return {
        "iteration": 1,
        "candidate_sha": candidate_sha,
        "artifact_tree_sha256": artifact_tree_sha256,
        "qa_status": "pass",
        "product_complete": product_complete,
        "verified_records": verified_records or [],
        "gap_records": gap_records or [],
        "planner_handoff": {
            "preservation_constraints": [],
            "update_targets": [],
            "validation_requirements": [],
        },
    }


def verified(claim_id: str, path: str, sha256: str = FRAME_SHA256) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "claim": f"{claim_id} works",
        "observations": [f"observed {claim_id}"],
        "execution_records": [
            {
                "type": "screenshot",
                "path": path,
                "sha256": sha256,
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


def manifest(path: str, sha256: str = FRAME_SHA256, status: str = "pass") -> dict[str, object]:
    return {"status": status, "artifacts": {path: sha256}}


def test_evidence_for_other_candidate_is_rejected(tmp_path: Path) -> None:
    """Dropping candidate-SHA comparison must make this fail."""
    raw = evidence(candidate_sha="c" * 40)

    with pytest.raises(EvidenceBindingError, match="candidate_sha"):
        EvidenceNormalizer(tmp_path).normalize(
            raw, EXPECTED_CANDIDATE, EXPECTED_TREE, {}
        )


def test_evidence_for_other_artifact_tree_is_rejected(tmp_path: Path) -> None:
    """Dropping artifact-tree comparison must make this fail."""
    raw = evidence(artifact_tree_sha256="c" * 64)

    with pytest.raises(EvidenceBindingError, match="artifact_tree_sha256"):
        EvidenceNormalizer(tmp_path).normalize(
            raw, EXPECTED_CANDIDATE, EXPECTED_TREE, {}
        )


def test_tampered_artifact_hash_is_rejected(tmp_path: Path) -> None:
    """Trusting QA's digest instead of hashing the file must make this fail."""
    (tmp_path / "frame.png").write_bytes(b"frame")
    raw = evidence(verified_records=[verified("player-moves", "frame.png", "0" * 64)])

    with pytest.raises(EvidenceBindingError, match="sha256"):
        EvidenceNormalizer(tmp_path, requirements()).normalize(
            raw,
            EXPECTED_CANDIDATE,
            EXPECTED_TREE,
            manifest("frame.png"),
        )


def test_missing_cited_artifact_is_rejected(tmp_path: Path) -> None:
    """Treating a manifest entry as proof that a file exists must make this fail."""
    raw = evidence(verified_records=[verified("player-moves", "missing.png")])

    with pytest.raises(EvidenceBindingError, match="does not exist"):
        EvidenceNormalizer(tmp_path, requirements()).normalize(
            raw,
            EXPECTED_CANDIDATE,
            EXPECTED_TREE,
            manifest("missing.png"),
        )


def test_cited_artifact_outside_evidence_root_is_rejected(tmp_path: Path) -> None:
    """Allowing a traversal path to cite host files must make this fail."""
    outside = tmp_path.parent / "outside-evidence.txt"
    outside.write_bytes(b"frame")
    raw = evidence(verified_records=[verified("player-moves", "../outside-evidence.txt")])

    with pytest.raises(EvidenceBindingError, match="outside evidence root"):
        EvidenceNormalizer(tmp_path, requirements()).normalize(
            raw,
            EXPECTED_CANDIDATE,
            EXPECTED_TREE,
            {"status": "pass", "artifacts": {}},
        )


def test_artifact_absent_from_adapter_manifest_is_rejected(tmp_path: Path) -> None:
    """Allowing QA to introduce an uncollected artifact must make this fail."""
    (tmp_path / "frame.png").write_bytes(b"frame")
    raw = evidence(verified_records=[verified("player-moves", "frame.png")])

    with pytest.raises(EvidenceBindingError, match="adapter manifest"):
        EvidenceNormalizer(tmp_path, requirements()).normalize(
            raw, EXPECTED_CANDIDATE, EXPECTED_TREE, {"status": "pass", "artifacts": {}}
        )


@pytest.mark.parametrize(
    ("verified_records", "gap_records", "message"),
    [
        (
            [
                verified("player-moves", "first.png"),
                verified("player-moves", "second.png"),
            ],
            [],
            "duplicate claim_id",
        ),
        (
            [verified("player-moves", "first.png")],
            [gap("player-moves")],
            "duplicate claim_id",
        ),
        ([verified("invented-claim", "first.png")], [], "unknown claim_id"),
    ],
)
def test_duplicate_or_unknown_claim_records_are_rejected(
    tmp_path: Path,
    verified_records: list[dict[str, object]],
    gap_records: list[dict[str, object]],
    message: str,
) -> None:
    """Losing registry identity checks must make one agent control issue identity."""
    (tmp_path / "first.png").write_bytes(b"frame")
    (tmp_path / "second.png").write_bytes(b"frame")
    raw = evidence(verified_records=verified_records, gap_records=gap_records)

    with pytest.raises(EvidenceBindingError, match=message):
        EvidenceNormalizer(tmp_path, requirements()).normalize(
            raw,
            EXPECTED_CANDIDATE,
            EXPECTED_TREE,
            {
                "status": "pass",
                "artifacts": {
                    "first.png": FRAME_SHA256,
                    "second.png": FRAME_SHA256,
                },
            },
        )


def test_host_completion_ignores_qa_completion_and_sorts_records(tmp_path: Path) -> None:
    """Trusting QA completion or retaining nondeterministic record order must fail."""
    (tmp_path / "frame.png").write_bytes(b"frame")
    raw = evidence(
        product_complete=False,
        verified_records=[
            verified("player-moves", "frame.png"),
            verified("optional-polish", "frame.png"),
        ],
    )
    original = deepcopy(raw)

    normalized = EvidenceNormalizer(tmp_path, requirements()).normalize(
        raw,
        EXPECTED_CANDIDATE,
        EXPECTED_TREE,
        manifest("frame.png"),
    )

    assert normalized["product_complete"] is True
    assert [record["claim_id"] for record in normalized["verified_records"]] == [
        "optional-polish",
        "player-moves",
    ]
    assert normalized["host_metadata"] == {
        "qa_product_complete": False,
        "deterministic_checks_passed": True,
        "required_claim_ids": ["player-moves"],
        "verified_required_claim_ids": ["player-moves"],
    }
    assert raw == original


@pytest.mark.parametrize(
    ("raw", "adapter_status"),
    [
        (evidence(verified_records=[]), "pass"),
        (
            evidence(
                verified_records=[verified("player-moves", "frame.png")],
                gap_records=[gap("optional-polish", "major")],
            ),
            "pass",
        ),
        (
            evidence(verified_records=[verified("player-moves", "frame.png")]),
            "fail",
        ),
    ],
)
def test_host_completion_requires_claims_no_major_gaps_and_passing_checks(
    tmp_path: Path, raw: dict[str, object], adapter_status: str
) -> None:
    """Omitting any host-owned completion gate must make this fail."""
    (tmp_path / "frame.png").write_bytes(b"frame")

    normalized = EvidenceNormalizer(tmp_path, requirements()).normalize(
        raw,
        EXPECTED_CANDIDATE,
        EXPECTED_TREE,
        manifest("frame.png", status=adapter_status),
    )

    assert normalized["product_complete"] is False


@pytest.mark.parametrize(
    "adapter_manifest",
    [
        {"artifacts": {"frame.png": FRAME_SHA256}},
        {"frame.png": FRAME_SHA256},
    ],
)
def test_host_completion_requires_affirmative_deterministic_check_status(
    tmp_path: Path, adapter_manifest: dict[str, object]
) -> None:
    """Defaulting a missing check status to pass must make this fail."""
    (tmp_path / "frame.png").write_bytes(b"frame")
    raw = evidence(verified_records=[verified("player-moves", "frame.png")])

    normalized = EvidenceNormalizer(tmp_path, requirements()).normalize(
        raw,
        EXPECTED_CANDIDATE,
        EXPECTED_TREE,
        adapter_manifest,
    )

    assert normalized["product_complete"] is False
    assert normalized["host_metadata"]["deterministic_checks_passed"] is False


def test_adapter_diagnostics_are_retained_separately_from_gaps(tmp_path: Path) -> None:
    """Mixing infrastructure diagnostics into product gaps must make this fail."""
    raw = evidence(product_complete=False)
    diagnostics = [
        {"category": "infrastructure", "code": "runner-offline", "message": "offline"}
    ]

    normalized = EvidenceNormalizer(tmp_path, requirements(required=())).normalize(
        raw,
        EXPECTED_CANDIDATE,
        EXPECTED_TREE,
        {"status": "blocked", "artifacts": {}, "diagnostics": diagnostics},
    )

    assert normalized["gap_records"] == []
    assert normalized["diagnostics"] == diagnostics


def test_completion_cannot_be_derived_without_a_requirements_registry(
    tmp_path: Path,
) -> None:
    """Treating an absent registry as zero required claims must make this fail."""
    raw = evidence(product_complete=True)

    with pytest.raises(EvidenceBindingError, match="requirements registry"):
        EvidenceNormalizer(tmp_path).normalize(
            raw, EXPECTED_CANDIDATE, EXPECTED_TREE, {}
        )


@pytest.mark.parametrize("severity", ["major ", "critical"])
def test_normalizer_rejects_noncanonical_gap_severity(
    tmp_path: Path, severity: str
) -> None:
    """Allowing severity aliases to bypass completion gates must make this fail."""
    raw = evidence(gap_records=[gap("player-moves", severity)])

    with pytest.raises(EvidenceBindingError, match="severity"):
        EvidenceNormalizer(tmp_path, requirements()).normalize(
            raw,
            EXPECTED_CANDIDATE,
            EXPECTED_TREE,
            {"status": "pass", "artifacts": {}},
        )
