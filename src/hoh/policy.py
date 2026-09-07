"""Host-owned progress measurement and terminal stop decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hoh.models import HarnessConfig


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
        previous_closed_count = 0
        previous_issue_statuses: dict[str, str] = {}
        no_progress = 0

        for loop in history:
            evidence = _evidence(loop)
            current_required = _verified_required_claim_ids(evidence)
            current_closed_issues = _closed_issue_ids(loop)
            current_issue_statuses = _issue_statuses(loop)
            current_acceptance = _supported_acceptance_claim_ids(loop, evidence)
            current_closed_count = _closed_issue_count(loop)
            newly_closed_existing_issue = any(
                status == "closed"
                and previous_issue_statuses.get(claim_id) in {"open", "regressed"}
                for claim_id, status in current_issue_statuses.items()
            )

            made_progress = bool(
                current_required - verified_required
                or newly_closed_existing_issue
                or current_acceptance - supported_acceptance
                or current_closed_count > previous_closed_count
            )
            no_progress = 0 if made_progress else no_progress + 1
            verified_required.update(current_required)
            closed_issues.update(current_closed_issues)
            supported_acceptance.update(current_acceptance)
            previous_closed_count = current_closed_count
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
        issue_summary: Mapping[str, object] | None = None,
        elapsed_seconds: int = 0,
        total_tokens: int = 0,
        cancelled: bool = False,
        unrecoverable_failure: str | None = None,
    ) -> StopDecision:
        """Return the first terminal outcome in the documented precedence order."""

        latest = latest_evidence or (_evidence(history[-1]) if history else {})
        latest_loop = history[-1] if history else {}
        checks_passed = (
            deterministic_checks_passed
            if deterministic_checks_passed is not None
            else _deterministic_checks_passed(latest_loop, latest)
        )
        summary = issue_summary or _mapping(latest_loop.get("issue_summary"))

        if _is_verified_completion(latest, checks_passed, summary, latest_loop):
            return StopDecision(True, "complete", "all required claims verified")
        if cancelled:
            return StopDecision(True, "cancelled", "run cancelled by user")

        failure = unrecoverable_failure or _unrecoverable_failure(latest_loop)
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


def _closed_issue_count(loop: Mapping[str, object]) -> int:
    summary = _mapping(loop.get("issue_summary"))
    value = summary.get("closed")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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
    issue_summary: Mapping[str, object],
    latest_loop: Mapping[str, object],
) -> bool:
    metadata = _mapping(evidence.get("host_metadata"))
    required = _string_ids(metadata.get("required_claim_ids"))
    verified_required = _string_ids(metadata.get("verified_required_claim_ids"))
    if (
        evidence.get("product_complete") is not True
        or checks_passed is not True
        or not required
        or not required <= verified_required
        or _has_blocking_gap(evidence)
    ):
        return False
    return not _has_open_blocking_issue(issue_summary, latest_loop)


def _has_blocking_gap(evidence: Mapping[str, object]) -> bool:
    return any(
        record.get("severity") in {"blocker", "major"}
        for record in _records(evidence.get("gap_records"))
    )


def _has_open_blocking_issue(
    issue_summary: Mapping[str, object], latest_loop: Mapping[str, object]
) -> bool:
    for issue in _records(latest_loop.get("issues")):
        if issue.get("status") in {"open", "regressed"} and issue.get("severity") in {
            "blocker",
            "major",
        }:
            return True
    for key in ("open_blocker", "open_major", "blocker", "major"):
        value = issue_summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    return False


def _unrecoverable_failure(loop: Mapping[str, object]) -> str | None:
    failure = _mapping(loop.get("failure"))
    category = failure.get("category")
    if category in {"infrastructure", "protocol"} and failure.get("repairable") is False:
        code = failure.get("code")
        return f"{category}:{code}" if isinstance(code, str) and code else str(category)
    return None


def _failure_decision(failure: str) -> StopDecision:
    category, separator, detail = failure.partition(":")
    if category not in {"infrastructure", "protocol"}:
        return StopDecision(True, "blocked", f"unrecoverable failure: {failure}")
    suffix = f": {detail}" if separator and detail else ""
    return StopDecision(True, "blocked", f"unrecoverable {category} failure{suffix}")
