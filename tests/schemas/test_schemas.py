from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest


@pytest.fixture
def schema_dir() -> Path:
    return Path(__file__).parents[2] / "src" / "hoh" / "resources" / "schemas"


def validate(payload: object, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    validator.check_schema(schema)
    validator.validate(payload)


def priority(identifier: str) -> dict[str, object]:
    return {
        "id": f"priority-{identifier}",
        "title": "Restore predictable boot",
        "implementation_target": "game/main.tscn",
        "acceptance_claims": ["boot"],
        "source_gap_ids": ["gap-boot"],
    }


def valid_plan() -> dict[str, object]:
    return {
        "iteration": 1,
        "objective": "Restore the boot flow.",
        "priorities": [priority("boot")],
        "preservation_constraints": ["Keep the menu reachable."],
        "acceptance_gate": ["The product boots without errors."],
    }


def valid_evidence() -> dict[str, object]:
    return {
        "iteration": 1,
        "candidate_sha": "a" * 40,
        "artifact_tree_sha256": "b" * 64,
        "qa_status": "pass",
        "product_complete": False,
        "verified_records": [
            {
                "claim_id": "boot",
                "claim": "The product boots.",
                "observations": ["The headless boot check passed."],
                "execution_records": [
                    {
                        "type": "command",
                        "path": "evidence/boot.log",
                        "sha256": "c" * 64,
                        "observation": "Exit status was zero.",
                    }
                ],
                "preservation_requirement": "Keep the product bootable.",
            }
        ],
        "gap_records": [
            {
                "claim_id": "playable",
                "severity": "major",
                "impact": "The user cannot start a session.",
                "observations": ["No start control was found."],
                "recommended_update": "Add a start control.",
                "validation_requirement": "Exercise the start control.",
            }
        ],
        "planner_handoff": {
            "preservation_constraints": ["Keep booting behavior."],
            "update_targets": ["game/menu.tscn"],
            "validation_requirements": ["Run the boot check."],
        },
    }


def test_all_schemas_are_valid_draft_2020_12_documents(schema_dir: Path) -> None:
    for schema_path in sorted(schema_dir.glob("*.schema.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)


def test_requirements_accepts_structured_claim_registry(schema_dir: Path) -> None:
    validate(
        {
            "schema_version": 1,
            "claims": [
                {"id": "boot", "description": "The product boots.", "required": True}
            ],
        },
        schema_dir / "requirements.schema.json",
    )


def test_requirements_rejects_incomplete_claim_and_unknown_fields(schema_dir: Path) -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate(
            {
                "schema_version": 1,
                "claims": [{"id": "boot", "description": "The product boots."}],
            },
            schema_dir / "requirements.schema.json",
        )


def test_requirements_rejects_identical_duplicate_claim_records(schema_dir: Path) -> None:
    claim = {"id": "boot", "description": "The product boots.", "required": True}

    with pytest.raises(jsonschema.ValidationError):
        validate(
            {"schema_version": 1, "claims": [claim, claim]},
            schema_dir / "requirements.schema.json",
        )


def test_plan_rejects_more_than_three_priorities(schema_dir: Path) -> None:
    plan = valid_plan()
    plan["priorities"] = [priority(str(index)) for index in range(4)]

    with pytest.raises(jsonschema.ValidationError):
        validate(plan, schema_dir / "plan.schema.json")


def test_plan_rejects_priority_without_acceptance_claims(schema_dir: Path) -> None:
    plan = valid_plan()
    plan["priorities"] = [
        {key: value for key, value in priority("boot").items() if key != "acceptance_claims"}
    ]

    with pytest.raises(jsonschema.ValidationError):
        validate(plan, schema_dir / "plan.schema.json")


def test_developer_report_requires_all_role_outputs(schema_dir: Path) -> None:
    report = {
        "summary": "Implemented the boot repair.",
        "completed_priority_ids": ["priority-boot"],
        "self_tests": ["python -m pytest -q"],
        "known_gaps": [],
    }
    validate(report, schema_dir / "developer-report.schema.json")

    report["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate(report, schema_dir / "developer-report.schema.json")


def test_verified_record_requires_candidate_bound_artifact(schema_dir: Path) -> None:
    evidence = valid_evidence()
    evidence["verified_records"][0]["execution_records"] = []  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        validate(evidence, schema_dir / "evidence.schema.json")


def test_evidence_rejects_execution_records_with_unknown_fields(schema_dir: Path) -> None:
    evidence = valid_evidence()
    evidence["verified_records"][0]["execution_records"][0]["extra"] = "not allowed"  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        validate(evidence, schema_dir / "evidence.schema.json")


def test_evidence_rejects_unknown_qa_status(schema_dir: Path) -> None:
    evidence = valid_evidence()
    evidence["qa_status"] = "unreviewed"

    with pytest.raises(jsonschema.ValidationError):
        validate(evidence, schema_dir / "evidence.schema.json")


def test_completed_evidence_requires_a_verified_record(schema_dir: Path) -> None:
    evidence = valid_evidence()
    evidence["product_complete"] = True
    evidence["verified_records"] = []

    with pytest.raises(jsonschema.ValidationError):
        validate(evidence, schema_dir / "evidence.schema.json")
