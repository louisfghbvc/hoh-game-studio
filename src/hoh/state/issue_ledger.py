"""Stable product issue identities and auditable status transitions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from hoh.state.store import atomic_write_json


class IssueLedgerError(ValueError):
    """Raised when persisted issue state is malformed."""


class IssueLedger:
    """Maintain claim-keyed product issues without trusting supplied counts."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, object]:
        """Load the ledger, returning an empty document before its first write."""

        if not self._path.exists():
            return {"schema_version": 1, "issues": []}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IssueLedgerError(f"could not read issue ledger: {self._path}") from error
        if not isinstance(document, dict) or not isinstance(document.get("issues"), list):
            raise IssueLedgerError("issue ledger must contain an issues list")
        return document

    def apply(self, evidence: Mapping[str, object], loop_index: int) -> None:
        """Apply one normalized evidence document and write derived counts."""

        if isinstance(loop_index, bool) or not isinstance(loop_index, int) or loop_index < 1:
            raise ValueError("loop_index must be a positive integer")
        candidate = evidence.get("candidate_sha")
        if not isinstance(candidate, str) or not candidate:
            raise IssueLedgerError("evidence candidate_sha must be non-empty")

        document = self.load()
        raw_issues = document["issues"]
        assert isinstance(raw_issues, list)
        issues: dict[str, dict[str, object]] = {}
        for issue in raw_issues:
            if not isinstance(issue, dict):
                raise IssueLedgerError("issue entries must be objects")
            claim_id = issue.get("claim_id")
            if not isinstance(claim_id, str) or not claim_id:
                raise IssueLedgerError("issue claim_id must be non-empty")
            if claim_id in issues:
                raise IssueLedgerError(f"duplicate issue claim_id: {claim_id}")
            history = issue.get("history")
            if not isinstance(history, list):
                raise IssueLedgerError(f"issue history must be a list: {claim_id}")
            issues[claim_id] = deepcopy(issue)

        for gap in _evidence_records(evidence, "gap_records"):
            claim_id = _record_claim_id(gap)
            issue = issues.get(claim_id)
            if issue is None:
                issue = {"claim_id": claim_id, "status": "open", "history": []}
                issues[claim_id] = issue
                status = "open"
            else:
                status = "regressed" if issue.get("status") == "closed" else str(
                    issue.get("status", "open")
                )
                if status not in {"open", "regressed"}:
                    status = "open"
            issue["status"] = status
            for key in (
                "severity",
                "impact",
                "recommended_update",
                "validation_requirement",
            ):
                if key in gap:
                    issue[key] = deepcopy(gap[key])
            _history(issue).append(
                {
                    "loop": loop_index,
                    "candidate": candidate,
                    "evidence_path": None,
                    "observation": _first_observation(gap),
                    "status": status,
                }
            )

        for verified in _evidence_records(evidence, "verified_records"):
            claim_id = _record_claim_id(verified)
            issue = issues.get(claim_id)
            if issue is None:
                continue
            issue["status"] = "closed"
            path, observation = _execution_provenance(verified)
            _history(issue).append(
                {
                    "loop": loop_index,
                    "candidate": candidate,
                    "evidence_path": path,
                    "observation": observation,
                    "status": "closed",
                }
            )

        ordered_issues = [issues[claim_id] for claim_id in sorted(issues)]
        updated: dict[str, object] = {
            "schema_version": 1,
            "issues": ordered_issues,
            "summary": _derived_summary(ordered_issues),
        }
        atomic_write_json(self._path, updated)

    def summary(self) -> dict[str, int]:
        """Derive counts from issue entries, ignoring any persisted summary."""

        document = self.load()
        issues = document["issues"]
        assert isinstance(issues, list)
        return _derived_summary(issues)


def _evidence_records(
    evidence: Mapping[str, object], field: str
) -> list[Mapping[str, object]]:
    records = evidence.get(field, [])
    if not isinstance(records, list) or any(not isinstance(record, Mapping) for record in records):
        raise IssueLedgerError(f"evidence {field} must be a list of objects")
    return records


def _record_claim_id(record: Mapping[str, object]) -> str:
    claim_id = record.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        raise IssueLedgerError("evidence claim_id must be non-empty")
    return claim_id


def _history(issue: dict[str, object]) -> list[object]:
    history = issue.get("history")
    if not isinstance(history, list):
        raise IssueLedgerError("issue history must be a list")
    return history


def _first_observation(record: Mapping[str, object]) -> str | None:
    observations = record.get("observations")
    if isinstance(observations, list) and observations and isinstance(observations[0], str):
        return observations[0]
    return None


def _execution_provenance(record: Mapping[str, object]) -> tuple[str | None, str | None]:
    execution_records = record.get("execution_records")
    if isinstance(execution_records, list) and execution_records:
        execution_record = execution_records[0]
        if isinstance(execution_record, Mapping):
            path = execution_record.get("path")
            observation = execution_record.get("observation")
            return (
                path if isinstance(path, str) else None,
                observation if isinstance(observation, str) else _first_observation(record),
            )
    return None, _first_observation(record)


def _derived_summary(issues: list[object]) -> dict[str, int]:
    counts = {"total": len(issues), "open": 0, "closed": 0, "regressed": 0}
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise IssueLedgerError("issue entries must be objects")
        status = issue.get("status")
        if status not in {"open", "closed", "regressed"}:
            raise IssueLedgerError(f"unknown issue status: {status}")
        counts[str(status)] += 1
    return counts
