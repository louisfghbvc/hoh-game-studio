"""Host-owned progress measurement and terminal stop decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hoh.models import HarnessConfig


_ISSUE_STATUSES = frozenset({"open", "closed", "regressed"})
_GAP_SEVERITIES = frozenset({"blocker", "major", "minor"})
_ISSUE_TEXT_FIELDS = ("impact", "recommended_update", "validation_requirement")
_ISSUE_FIELDS = frozenset({"claim_id", "status", "severity", "history", *_ISSUE_TEXT_FIELDS})
_HISTORY_FIELDS = frozenset(
    {"loop", "candidate", "evidence_path", "observation", "status"}
)
_STATUS_TRANSITIONS = {
    None: frozenset({"open"}),
    "open": frozenset({"open", "closed"}),
    "closed": frozenset({"closed", "regressed"}),
    "regressed": frozenset({"regressed", "closed"}),
}
_MISSING = object()


@dataclass(frozen=True)
class ProgressSnapshot:
    """Evidence-derived progress at the end of a loop history."""

    verified_required_claim_ids: frozenset[str]
    closed_issue_ids: frozenset[str]
    supported_acceptance_claim_ids: frozenset[str]
    made_progress: bool
    consecutive_no_progress: int
    consecutive_same_blocker: int
    repeated_blocker_ids: frozenset[str]


@dataclass(frozen=True)
class StopDecision:
    should_stop: bool
    terminal_status: str | None
    reason: str


class StopPolicy:
    """Apply bounded, deterministic stopping rules to host-owned loop records."""

    def __init__(self, config: HarnessConfig) -> None:
        self._config = config

    def progress(self, history: Sequence[Mapping[str, object]]) -> ProgressSnapshot:
        """Measure only new, host-verified evidence across completed loops."""

        verified_required: set[str] = set()
        closed_issues: set[str] = set()
        supported_acceptance: set[str] = set()
        previous_issue_statuses: dict[str, str] = {}
        no_progress = 0

        for loop in history:
            evidence = _evidence(loop)
            current_required = _verified_required_claim_ids(evidence)
            current_closed_issues = _closed_issue_ids(loop)
            current_issue_statuses = _issue_statuses(loop)
            current_acceptance = _supported_acceptance_claim_ids(loop, evidence)
            newly_closed_existing_issue = any(
                status == "closed"
                and previous_issue_statuses.get(claim_id) in {"open", "regressed"}
                for claim_id, status in current_issue_statuses.items()
            )

            made_progress = bool(
                current_required - verified_required
                or newly_closed_existing_issue
                or current_acceptance - supported_acceptance
            )
            no_progress = 0 if made_progress else no_progress + 1
            verified_required.update(current_required)
            closed_issues.update(current_closed_issues)
            supported_acceptance.update(current_acceptance)
            previous_issue_statuses = current_issue_statuses

        blockers, blocker_streak = _trailing_blockers(history)
        return ProgressSnapshot(
            verified_required_claim_ids=frozenset(verified_required),
            closed_issue_ids=frozenset(closed_issues),
            supported_acceptance_claim_ids=frozenset(supported_acceptance),
            made_progress=no_progress == 0 and bool(history),
            consecutive_no_progress=no_progress,
            consecutive_same_blocker=blocker_streak,
            repeated_blocker_ids=frozenset(blockers),
        )

    def evaluate(
        self,
        history: Sequence[Mapping[str, object]] = (),
        *,
        latest_evidence: Mapping[str, object] | None = None,
        deterministic_checks_passed: bool | None = None,
        deterministic_checks_candidate_sha: str | None = None,
        issue_summary: Mapping[str, object] | None = None,
        issue_state_candidate_sha: str | None = None,
        qa_invocation_id: str | None = None,
        release_gate: Mapping[str, object] | None = None,
        elapsed_seconds: int = 0,
        total_tokens: int = 0,
        cancelled: bool = False,
        unrecoverable_failure: str | None = None,
    ) -> StopDecision:
        """Return the first terminal outcome in the documented precedence order.

        Task 13 should normally pass a completed loop record in ``history``.  Its
        latest record must contain normalized evidence with ``candidate_sha`` and
        ``qa_status``, the ordinary ``qa_invocation_id``, check and issue-state
        candidate SHAs, authoritative issue entries (or a severity-complete
        summary), and a ``release_gate`` record. The gate record must identify a
        different invocation, the same candidate, ``full_release`` scope or kind,
        QA pass, end-to-end pass, and deterministic-check pass. When passing
        ``latest_evidence`` directly, pass the matching candidate-bound check,
        issue-summary, QA invocation ID, and release gate arguments as well;
        completion otherwise fails closed.
        """

        latest_loop = history[-1] if latest_evidence is None and history else {}
        latest = latest_evidence if latest_evidence is not None else _evidence(latest_loop)
        checks_passed = (
            deterministic_checks_passed
            if deterministic_checks_passed is not None
            else _deterministic_checks_passed(latest_loop, latest)
        )
        checks_candidate = (
            deterministic_checks_candidate_sha
            if deterministic_checks_candidate_sha is not None
            else latest_loop.get("deterministic_checks_candidate_sha")
        )
        summary = issue_summary if issue_summary is not None else _mapping(
            latest_loop.get("issue_summary")
        )
        issue_candidate = (
            issue_state_candidate_sha
            if issue_state_candidate_sha is not None
            else latest_loop.get("issues_candidate_sha")
        )
        qa_invocation = (
            qa_invocation_id
            if qa_invocation_id is not None
            else latest_loop.get("qa_invocation_id")
        )
        gate = (
            release_gate
            if release_gate is not None
            else _mapping(latest_loop.get("release_gate"))
        )

        if _is_verified_completion(
            latest,
            checks_passed,
            checks_candidate,
            summary,
            issue_candidate,
            latest_loop,
            qa_invocation,
            gate,
        ):
            return StopDecision(True, "complete", "all required claims verified")
        if cancelled:
            return StopDecision(True, "cancelled", "run cancelled by user")

        failure = (
            _validated_failure(unrecoverable_failure)
            if unrecoverable_failure is not None
            else _unrecoverable_failure(latest_loop)
        )
        if failure is not None:
            return _failure_decision(failure)

        snapshot = self.progress(history)
        if snapshot.consecutive_same_blocker >= self._config.max_consecutive_same_blocker:
            return StopDecision(
                True,
                "blocked",
                "same blocker persisted for "
                f"{self._config.max_consecutive_same_blocker} consecutive loops",
            )
        if snapshot.consecutive_no_progress >= self._config.max_consecutive_no_progress:
            return StopDecision(
                True,
                "blocked",
                "no measurable evidence progress for "
                f"{self._config.max_consecutive_no_progress} consecutive loops",
            )
        if total_tokens >= self._config.max_total_tokens:
            return StopDecision(True, "budget_exhausted", "token budget exhausted")
        if elapsed_seconds >= self._config.max_elapsed_minutes * 60:
            return StopDecision(True, "budget_exhausted", "elapsed-time budget exhausted")
        if len(history) >= self._config.max_loops:
            return StopDecision(True, "budget_exhausted", "loop budget exhausted")
        return StopDecision(False, None, "continue")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _records(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(record for record in value if isinstance(record, Mapping))


def _string_ids(value: object) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


def _evidence(loop: Mapping[str, object]) -> Mapping[str, object]:
    nested = loop.get("normalized_evidence")
    if isinstance(nested, Mapping):
        return nested
    return loop


def _verified_claim_ids(evidence: Mapping[str, object]) -> frozenset[str]:
    return frozenset(
        claim_id
        for record in _records(evidence.get("verified_records"))
        if isinstance((claim_id := record.get("claim_id")), str) and claim_id
    )


def _verified_required_claim_ids(evidence: Mapping[str, object]) -> frozenset[str]:
    metadata = _mapping(evidence.get("host_metadata"))
    required = _string_ids(metadata.get("required_claim_ids"))
    verified = _string_ids(metadata.get("verified_required_claim_ids"))
    return required & verified


def _closed_issue_ids(loop: Mapping[str, object]) -> frozenset[str]:
    issues = _records(loop.get("issues"))
    return frozenset(
        claim_id
        for issue in issues
        if issue.get("status") == "closed"
        and isinstance((claim_id := issue.get("claim_id")), str)
        and claim_id
    )


def _issue_statuses(loop: Mapping[str, object]) -> dict[str, str]:
    return {
        claim_id: status
        for issue in _records(loop.get("issues"))
        if isinstance((claim_id := issue.get("claim_id")), str)
        and claim_id
        and isinstance((status := issue.get("status")), str)
    }


def _supported_acceptance_claim_ids(
    loop: Mapping[str, object], evidence: Mapping[str, object]
) -> frozenset[str]:
    acceptance = _string_ids(loop.get("acceptance_claim_ids"))
    return acceptance & _verified_claim_ids(evidence)


def _blocker_ids(loop: Mapping[str, object]) -> frozenset[str]:
    return frozenset(
        claim_id
        for record in _records(_evidence(loop).get("gap_records"))
        if record.get("severity") == "blocker"
        and isinstance((claim_id := record.get("claim_id")), str)
        and claim_id
    )


def _trailing_blockers(
    history: Sequence[Mapping[str, object]],
) -> tuple[frozenset[str], int]:
    shared: set[str] | None = None
    streak = 0
    for loop in reversed(history):
        blockers = _blocker_ids(loop)
        shared = set(blockers) if shared is None else shared & blockers
        if not shared:
            break
        streak += 1
    return frozenset(shared or ()), streak


def _deterministic_checks_passed(
    loop: Mapping[str, object], evidence: Mapping[str, object]
) -> bool:
    value = loop.get("deterministic_checks_passed")
    if isinstance(value, bool):
        return value
    value = _mapping(evidence.get("host_metadata")).get("deterministic_checks_passed")
    return value is True


def _is_verified_completion(
    evidence: Mapping[str, object],
    checks_passed: bool,
    checks_candidate_sha: object,
    issue_summary: Mapping[str, object],
    issue_state_candidate_sha: object,
    latest_loop: Mapping[str, object],
    qa_invocation_id: object,
    release_gate: Mapping[str, object],
) -> bool:
    metadata = _mapping(evidence.get("host_metadata"))
    candidate_sha = evidence.get("candidate_sha")
    required = _string_ids(metadata.get("required_claim_ids"))
    verified_required = _string_ids(metadata.get("verified_required_claim_ids"))
    if (
        evidence.get("product_complete") is not True
        or evidence.get("qa_status") != "pass"
        or checks_passed is not True
        or not isinstance(candidate_sha, str)
        or not candidate_sha
        or checks_candidate_sha != candidate_sha
        or not required
        or not required <= verified_required
        or _has_blocking_gap(evidence)
        or not _valid_release_gate(release_gate, candidate_sha, qa_invocation_id)
    ):
        return False
    return _issue_state_is_complete(
        issue_summary, issue_state_candidate_sha, latest_loop, candidate_sha
    )


def _has_blocking_gap(evidence: Mapping[str, object]) -> bool:
    return any(
        record.get("severity") in {"blocker", "major"}
        for record in _records(evidence.get("gap_records"))
    )


def _issue_state_is_complete(
    issue_summary: Mapping[str, object],
    issue_state_candidate_sha: object,
    latest_loop: Mapping[str, object],
    candidate_sha: str,
) -> bool:
    issues = latest_loop.get("issues", _MISSING)
    if issues is not _MISSING:
        if issue_state_candidate_sha != candidate_sha:
            return False
        return _valid_issue_entries(issues)

    if issue_state_candidate_sha != candidate_sha:
        return False
    return (
        issue_summary.get("candidate_sha") == candidate_sha
        and issue_summary.get("severity_complete") is True
        and _nonnegative_integer(issue_summary.get("open_blocker")) == 0
        and _nonnegative_integer(issue_summary.get("open_major")) == 0
    )


def _valid_release_gate(
    release_gate: Mapping[str, object], candidate_sha: str, qa_invocation_id: object
) -> bool:
    invocation_id = release_gate.get("invocation_id")
    scope = release_gate.get("scope", release_gate.get("kind"))
    return (
        isinstance(qa_invocation_id, str)
        and bool(qa_invocation_id)
        and isinstance(invocation_id, str)
        and bool(invocation_id)
        and invocation_id != qa_invocation_id
        and scope == "full_release"
        and release_gate.get("qa_status") == "pass"
        and release_gate.get("end_to_end_passed") is True
        and release_gate.get("deterministic_checks_passed") is True
        and release_gate.get("candidate_sha") == candidate_sha
    )


def _valid_issue_entries(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return False
    seen_claim_ids: set[str] = set()
    for issue in value:
        if not isinstance(issue, Mapping) or set(issue) != _ISSUE_FIELDS:
            return False
        claim_id = issue.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id or claim_id in seen_claim_ids:
            return False
        seen_claim_ids.add(claim_id)
        status = issue.get("status")
        if status not in _ISSUE_STATUSES or issue.get("severity") not in _GAP_SEVERITIES:
            return False
        if any(
            not isinstance(issue.get(field), str) or not issue[field]
            for field in _ISSUE_TEXT_FIELDS
        ):
            return False
        if not _valid_issue_history(issue.get("history"), str(status)):
            return False
        if status in {"open", "regressed"} and issue.get("severity") in {
            "blocker",
            "major",
        }:
            return False
    return True


def _valid_issue_history(value: object, current_status: str) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return False
    if not value:
        return False
    previous_loop = 0
    previous_status: str | None = None
    for event in value:
        if not isinstance(event, Mapping) or set(event) != _HISTORY_FIELDS:
            return False
        loop_index = event.get("loop")
        if (
            isinstance(loop_index, bool)
            or not isinstance(loop_index, int)
            or loop_index <= previous_loop
            or not _is_candidate_sha(event.get("candidate"))
        ):
            return False
        status = event.get("status")
        if status not in _ISSUE_STATUSES or status not in _STATUS_TRANSITIONS[previous_status]:
            return False
        evidence_path = event.get("evidence_path")
        if status == "closed":
            if not isinstance(evidence_path, str) or not evidence_path:
                return False
        elif evidence_path is not None:
            return False
        if not isinstance(event.get("observation"), str) or not event["observation"]:
            return False
        previous_loop = loop_index
        previous_status = str(status)
    return previous_status == current_status


def _is_candidate_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and 40 <= len(value) <= 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonnegative_integer(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _unrecoverable_failure(loop: Mapping[str, object]) -> str | None:
    failure = _mapping(loop.get("failure"))
    category = failure.get("category")
    if category in {"infrastructure", "protocol"} and failure.get("repairable") is False:
        code = failure.get("code")
        return f"{category}:{code}" if isinstance(code, str) and code else str(category)
    return None


def _validated_failure(failure: str) -> str | None:
    category, separator, detail = failure.partition(":")
    if category not in {"infrastructure", "protocol"}:
        return None
    if separator and not detail:
        return None
    return failure


def _failure_decision(failure: str) -> StopDecision:
    category, separator, detail = failure.partition(":")
    assert category in {"infrastructure", "protocol"}
    suffix = f": {detail}" if separator and detail else ""
    return StopDecision(True, "blocked", f"unrecoverable {category} failure{suffix}")
