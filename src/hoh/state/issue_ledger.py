"""Stable product issue identities and auditable status transitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path

from hoh.state.store import atomic_write_json


_ISSUE_STATUSES = frozenset({"open", "closed", "regressed"})
_GAP_SEVERITIES = frozenset({"blocker", "major", "minor"})
_ISSUE_TEXT_FIELDS = ("impact", "recommended_update", "validation_requirement")
_CURRENT_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_CORRELATION_START_FIELD = "application_correlation_start_loop"
_MISSING = object()
_HISTORY_FIELDS = frozenset(
    {"loop", "candidate", "evidence_path", "observation", "status"}
)
_STATUS_TRANSITIONS = {
    None: frozenset({"open"}),
    "open": frozenset({"open", "closed"}),
    "closed": frozenset({"closed", "regressed"}),
    "regressed": frozenset({"regressed", "closed"}),
}


class IssueLedgerError(ValueError):
    """Raised when persisted issue state is malformed."""


class IssueLedger:
    """Maintain claim-keyed product issues without trusting supplied counts."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, object]:
        """Load the ledger, returning an empty document before its first write."""

        if not self._path.exists():
            return {
                "schema_version": 1,
                "issues": [],
                "applications": [],
                "summary": _derived_summary([]),
            }
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IssueLedgerError(f"could not read issue ledger: {self._path}") from error
        if not isinstance(document, dict) or not isinstance(document.get("issues"), list):
            raise IssueLedgerError("issue ledger must contain an issues list")
        document["applications"] = _validated_applications(
            document.get("applications", [])
        )
        correlation_start_loop = _validated_correlation_start_loop(
            document, document["applications"]
        )
        if correlation_start_loop is not None:
            document[_CORRELATION_START_FIELD] = correlation_start_loop
        document["summary"] = _derived_summary(document["issues"])
        return document

    def apply(self, evidence: Mapping[str, object], loop_index: int) -> None:
        """Apply one normalized evidence document and write derived counts."""

        if isinstance(loop_index, bool) or not isinstance(loop_index, int) or loop_index < 1:
            raise ValueError("loop_index must be a positive integer")
        candidate = evidence.get("candidate_sha")
        if not isinstance(candidate, str) or not candidate:
            raise IssueLedgerError("evidence candidate_sha must be non-empty")
        evidence_sha256 = _canonical_evidence_sha256(evidence)

        document = self.load()
        raw_issues = document["issues"]
        assert isinstance(raw_issues, list)
        applications = document["applications"]
        assert isinstance(applications, list)
        correlation_start_loop = document.get(_CORRELATION_START_FIELD)
        assert correlation_start_loop is None or isinstance(
            correlation_start_loop, int
        )
        applications_by_loop = {
            application["loop"]: application for application in applications
        }
        issues: dict[str, dict[str, object]] = {}
        history_loop_indices: list[int] = []
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
            status = issue.get("status")
            severity = issue.get("severity")
            if status not in _ISSUE_STATUSES:
                raise IssueLedgerError(f"issue status is invalid: {claim_id}")
            if severity not in _GAP_SEVERITIES:
                raise IssueLedgerError(f"issue severity is invalid: {claim_id}")
            for field in _ISSUE_TEXT_FIELDS:
                value = issue.get(field)
                if not isinstance(value, str) or not value:
                    raise IssueLedgerError(f"issue {field} must be non-empty: {claim_id}")
            history_loop_indices.extend(
                _validated_history(
                    history,
                    str(status),
                    applications_by_loop,
                    correlation_start_loop,
                )
            )
            issues[claim_id] = deepcopy(issue)

        replay = next(
            (application for application in applications if application["loop"] == loop_index),
            None,
        )
        if replay is not None:
            if (
                replay["candidate"] == candidate
                and replay["evidence_sha256"] == evidence_sha256
            ):
                return
            raise IssueLedgerError(
                f"same loop {loop_index} already has a different application identity"
            )

        latest_loop = max(
            [application["loop"] for application in applications]
            + history_loop_indices,
            default=0,
        )
        if loop_index == latest_loop:
            raise IssueLedgerError(
                f"same loop {loop_index} has state without a matching application identity"
            )
        if loop_index < latest_loop:
            raise IssueLedgerError(
                f"loop {loop_index} is out of order after loop {latest_loop}"
            )
        if correlation_start_loop is None:
            correlation_start_loop = loop_index if history_loop_indices else 1

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
        applications.append(
            {
                "loop": loop_index,
                "candidate": candidate,
                "evidence_sha256": evidence_sha256,
            }
        )
        updated: dict[str, object] = {
            "schema_version": _CURRENT_SCHEMA_VERSION,
            _CORRELATION_START_FIELD: correlation_start_loop,
            "issues": ordered_issues,
            "applications": applications,
            "summary": _derived_summary(ordered_issues),
        }
        atomic_write_json(self._path, updated)

    def summary(self) -> dict[str, int]:
        """Derive counts from issue entries, ignoring any persisted summary."""

        document = self.load()
        issues = document["issues"]
        assert isinstance(issues, list)
        return _derived_summary(issues)

    def validate_replay(
        self, evidence_by_loop: Sequence[tuple[int, Mapping[str, object]]]
    ) -> dict[str, object]:
        """Read-only check that ledger applications match normalized evidence."""

        expected: list[dict[str, object]] = []
        for loop_index, evidence in evidence_by_loop:
            if (
                isinstance(loop_index, bool)
                or not isinstance(loop_index, int)
                or loop_index < 1
            ):
                raise IssueLedgerError("loop_index must be a positive integer")
            candidate = evidence.get("candidate_sha")
            if not isinstance(candidate, str) or not candidate:
                raise IssueLedgerError("evidence candidate_sha must be non-empty")
            expected.append(
                {
                    "loop": loop_index,
                    "candidate": candidate,
                    "evidence_sha256": _canonical_evidence_sha256(evidence),
                }
            )
        document = self.load()
        applications = document.get("applications")
        if applications != expected:
            raise IssueLedgerError(
                "issue ledger applications do not match durable evidence replay"
            )
        return document


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
        if status not in _ISSUE_STATUSES:
            raise IssueLedgerError(f"unknown issue status: {status}")
        counts[str(status)] += 1
    return counts


def _canonical_evidence_sha256(evidence: Mapping[str, object]) -> str:
    try:
        canonical = json.dumps(
            dict(evidence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise IssueLedgerError("evidence must be canonical JSON data") from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_applications(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise IssueLedgerError("issue ledger applications must be a list")
    applications: list[dict[str, object]] = []
    previous_loop = 0
    for application in raw:
        if not isinstance(application, dict):
            raise IssueLedgerError("issue ledger application must be an object")
        loop_index = application.get("loop")
        candidate = application.get("candidate")
        evidence_sha256 = application.get("evidence_sha256")
        if (
            isinstance(loop_index, bool)
            or not isinstance(loop_index, int)
            or loop_index <= previous_loop
        ):
            raise IssueLedgerError("issue ledger application loops must strictly increase")
        if not _is_candidate_sha(candidate):
            raise IssueLedgerError("issue ledger application candidate SHA is invalid")
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise IssueLedgerError("issue ledger application evidence_sha256 is invalid")
        applications.append(deepcopy(application))
        previous_loop = loop_index
    return applications


def _validated_correlation_start_loop(
    document: Mapping[str, object], applications: list[dict[str, object]]
) -> int | None:
    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in {_LEGACY_SCHEMA_VERSION, _CURRENT_SCHEMA_VERSION}
    ):
        raise IssueLedgerError("issue ledger schema_version is invalid")

    raw_start_loop = document.get(_CORRELATION_START_FIELD, _MISSING)
    if raw_start_loop is not _MISSING and (
        isinstance(raw_start_loop, bool)
        or not isinstance(raw_start_loop, int)
        or raw_start_loop < 1
    ):
        raise IssueLedgerError(
            "issue ledger application correlation boundary must be a positive integer"
        )

    if schema_version == _LEGACY_SCHEMA_VERSION:
        if applications:
            # A v1 document cannot authenticate history before its first application.
            # Requiring correlation from loop 1 accepts fully correlated native state
            # while rejecting an ambiguous or injected pre-application prefix.
            return 1
        if raw_start_loop is _MISSING:
            return None

    if raw_start_loop is _MISSING:
        raise IssueLedgerError(
            "issue ledger application correlation boundary is missing"
        )
    if not applications:
        raise IssueLedgerError(
            "issue ledger application correlation boundary has no application"
        )
    if applications[0]["loop"] < raw_start_loop:
        raise IssueLedgerError(
            "issue ledger application precedes its correlation boundary"
        )
    return raw_start_loop


def _validated_history(
    history: list[object],
    current_status: str,
    applications_by_loop: Mapping[object, Mapping[str, object]],
    correlation_start_loop: int | None,
) -> list[int]:
    if not history:
        raise IssueLedgerError("issue history must not be empty")
    loop_indices: list[int] = []
    previous_loop = 0
    previous_status: str | None = None
    for event in history:
        if not isinstance(event, Mapping):
            raise IssueLedgerError("issue history event must be an object")
        if set(event) != _HISTORY_FIELDS:
            raise IssueLedgerError("issue history event fields are invalid")
        loop_index = event.get("loop")
        if (
            isinstance(loop_index, bool)
            or not isinstance(loop_index, int)
            or loop_index <= previous_loop
        ):
            raise IssueLedgerError(
                "issue history loops must be positive and strictly increasing"
            )
        candidate = event.get("candidate")
        if not _is_candidate_sha(candidate):
            raise IssueLedgerError("issue history candidate SHA is invalid")
        if (
            correlation_start_loop is not None
            and loop_index >= correlation_start_loop
        ):
            application = applications_by_loop.get(loop_index)
            if application is None:
                raise IssueLedgerError(
                    f"issue history loop {loop_index} has no application"
                )
            if application.get("candidate") != candidate:
                raise IssueLedgerError(
                    "issue history candidate does not match its application"
                )
        status = event.get("status")
        if status not in _ISSUE_STATUSES:
            raise IssueLedgerError("issue history status is invalid")
        if status not in _STATUS_TRANSITIONS[previous_status]:
            raise IssueLedgerError("issue history status transition is invalid")
        evidence_path = event.get("evidence_path")
        if status == "closed":
            if not isinstance(evidence_path, str) or not evidence_path:
                raise IssueLedgerError(
                    "issue history evidence_path must identify closed evidence"
                )
        elif evidence_path is not None:
            raise IssueLedgerError("issue history evidence_path must be null for a gap")
        observation = event.get("observation")
        if not isinstance(observation, str) or not observation:
            raise IssueLedgerError("issue history observation must be non-empty")
        loop_indices.append(loop_index)
        previous_loop = loop_index
        previous_status = str(status)
    if previous_status != current_status:
        raise IssueLedgerError("issue status does not match final history status")
    return loop_indices


def _is_candidate_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and 40 <= len(value) <= 64
        and all(character in "0123456789abcdef" for character in value)
    )
