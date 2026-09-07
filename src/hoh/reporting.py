"""Deterministic machine status and human-readable HoH run reports."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from hoh.policy import StopDecision


_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_FAILURE_CATEGORIES = frozenset({"infrastructure", "protocol"})


def build_status(
    run_state: Mapping[str, object],
    *,
    receipts: Iterable[Mapping[str, object]] = (),
    decision: StopDecision | Mapping[str, object] | None = None,
    issue_ledger: Mapping[str, object] | object | None = None,
) -> dict[str, object]:
    """Build a JSON-safe status document from host-owned structured records.

    Receipt usage and issue totals are recomputed from their individual records;
    aggregate fields in persisted state are intentionally ignored.
    """

    loops = _records(run_state.get("loops"))
    receipt_records = tuple(receipt for receipt in receipts if isinstance(receipt, Mapping))
    resolved_decision = _decision_fields(decision)
    issues = _issues(issue_ledger)
    usage_by_role, role_attempts, total_tokens = _usage(receipt_records)
    verified_claim_ids = _verified_claim_ids(loops)
    gaps = _remaining_gaps(issues)
    failures = _failures(run_state, loops, resolved_decision)
    terminal_status = resolved_decision["terminal_status"]
    run_id = _text(run_state.get("run_id"))
    best_candidate = _candidate(run_state, "best_candidate")

    status: dict[str, object] = {
        "run_id": run_id,
        "terminal_status": terminal_status,
        "reason": resolved_decision["reason"],
        "start_sha": _text(run_state.get("start_sha")),
        "current_candidate": _candidate(run_state, "current_candidate"),
        "best_candidate": best_candidate,
        "completed_loops": len(loops),
        "role_attempts": role_attempts,
        "usage_by_role": usage_by_role,
        "total_tokens": total_tokens,
        "elapsed_seconds": _nonnegative_int(run_state.get("elapsed_seconds")),
        "verified_claim_ids": verified_claim_ids,
        "remaining_gaps": gaps,
        "issue_summary": _issue_summary(issues),
        "failures": failures,
        "failure_category": failures[0]["category"] if failures else None,
        "skills": _skills(run_state),
        "guidance": _guidance(terminal_status, run_id, best_candidate),
    }
    return status


def render_run_summary(status: Mapping[str, object]) -> str:
    """Render a stable, human-readable report from a machine status document."""

    terminal_status = _text(status.get("terminal_status")) or "running"
    lines = [
        "# HoH Run Summary",
        "",
        f"Run ID: {_display(status.get('run_id'))}",
        f"Status: {terminal_status}",
        f"Reason: {_display(status.get('reason'))}",
        f"Start SHA: {_display(status.get('start_sha'))}",
        f"Current candidate: {_display(status.get('current_candidate'))}",
        f"Best candidate: {_display(status.get('best_candidate'))}",
        f"Completed loops: {_display(status.get('completed_loops'))}",
        f"Elapsed seconds: {_display(status.get('elapsed_seconds'))}",
        f"Total tokens: {_display(status.get('total_tokens'))}",
        "",
        "## Role attempts and usage",
    ]
    attempts = _mapping(status.get("role_attempts"))
    usage = _mapping(status.get("usage_by_role"))
    for role in sorted(set(attempts) | set(usage)):
        usage_record = _mapping(usage.get(role))
        lines.append(
            f"- {role}: attempts={_display(attempts.get(role))}, "
            f"tokens={_display(usage_record.get('total_tokens'))}, "
            f"input={_display(usage_record.get('input_tokens'))}, "
            f"cached_input={_display(usage_record.get('cached_input_tokens'))}, "
            f"output={_display(usage_record.get('output_tokens'))}, "
            f"reasoning={_display(usage_record.get('reasoning_output_tokens'))}"
        )
    if not attempts and not usage:
        lines.append("- none")

    lines.extend(("", "## Verified claims"))
    lines.extend(_bullet_values(status.get("verified_claim_ids")))

    lines.extend(("", "## Remaining gaps"))
    gaps = _records(status.get("remaining_gaps"))
    for gap in sorted(gaps, key=_claim_id):
        lines.append(
            f"- {_display(gap.get('claim_id'))}: "
            f"status={_display(gap.get('status'))}, severity={_display(gap.get('severity'))}"
        )
    if not gaps:
        lines.append("- none")

    lines.extend(("", "## Infrastructure and protocol failures"))
    failures = _records(status.get("failures"))
    for failure in sorted(failures, key=_failure_key):
        category = _display(failure.get("category"))
        detail = _text(failure.get("code")) or _text(failure.get("reason")) or "unknown"
        lines.append(f"- {category}: {detail}")
    if not failures:
        lines.append("- none")

    lines.extend(("", "## Skills"))
    skills = _records(status.get("skills"))
    for skill in sorted(skills, key=_skill_key):
        lines.append(f"- {_display(skill.get('skill_id'))}: {_display(skill.get('sha256'))}")
    if not skills:
        lines.append("- none")

    lines.extend(("", "## Next action", _display(status.get("guidance")), ""))
    return "\n".join(lines)


def write_run_summary(run_dir: Path, summary: str) -> Path:
    """Atomically write the terminal report beneath its host-owned run directory."""

    path = Path(run_dir) / "run-summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as report_file:
            temporary = Path(report_file.name)
            report_file.write(summary)
            report_file.flush()
            os.fsync(report_file.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return path


def _decision_fields(
    decision: StopDecision | Mapping[str, object] | None,
) -> dict[str, object]:
    if isinstance(decision, StopDecision):
        return {"terminal_status": decision.terminal_status, "reason": decision.reason}
    if isinstance(decision, Mapping):
        return {
            "terminal_status": _text(decision.get("terminal_status")),
            "reason": _text(decision.get("reason")),
        }
    return {"terminal_status": None, "reason": None}


def _usage(
    receipts: Sequence[Mapping[str, object]],
) -> tuple[dict[str, dict[str, int]], dict[str, int], int]:
    usage_by_role: dict[str, dict[str, int]] = {}
    role_attempts: dict[str, int] = {}
    for receipt in receipts:
        role = _text(receipt.get("role"))
        if role is None:
            continue
        usage = _mapping(receipt.get("usage"))
        if not usage:
            usage = receipt
        totals = usage_by_role.setdefault(role, {field: 0 for field in _USAGE_FIELDS})
        role_attempts[role] = role_attempts.get(role, 0) + 1
        for field in _USAGE_FIELDS:
            totals[field] += _nonnegative_int(usage.get(field))
    for totals in usage_by_role.values():
        totals["total_tokens"] = (
            totals["input_tokens"]
            + totals["output_tokens"]
            + totals["reasoning_output_tokens"]
        )
    ordered_usage = {role: usage_by_role[role] for role in sorted(usage_by_role)}
    ordered_attempts = {role: role_attempts[role] for role in sorted(role_attempts)}
    return ordered_usage, ordered_attempts, sum(
        totals["total_tokens"] for totals in ordered_usage.values()
    )


def _issues(issue_ledger: Mapping[str, object] | object | None) -> tuple[Mapping[str, object], ...]:
    if issue_ledger is None:
        return ()
    document: object = issue_ledger
    load = getattr(issue_ledger, "load", None)
    if callable(load):
        document = load()
    return _records(_mapping(document).get("issues"))


def _issue_summary(issues: Sequence[Mapping[str, object]]) -> dict[str, int]:
    summary = {"total": len(issues), "open": 0, "closed": 0, "regressed": 0}
    for issue in issues:
        status = _text(issue.get("status"))
        if status in {"open", "closed", "regressed"}:
            summary[status] += 1
    return summary


def _verified_claim_ids(loops: Sequence[Mapping[str, object]]) -> list[str]:
    return sorted(
        {
            claim_id
            for loop in loops
            for record in _records(_evidence(loop).get("verified_records"))
            if (claim_id := _text(record.get("claim_id"))) is not None
        }
    )


def _remaining_gaps(issues: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        dict(issue)
        for issue in sorted(issues, key=_claim_id)
        if _text(issue.get("status")) in {"open", "regressed"}
    ]


def _failures(
    run_state: Mapping[str, object],
    loops: Sequence[Mapping[str, object]],
    decision: Mapping[str, object],
) -> list[dict[str, object]]:
    diagnostics = [
        diagnostic
        for document in (run_state, *loops)
        for diagnostic in _records(_evidence(document).get("diagnostics"))
        if _text(diagnostic.get("category")) in _FAILURE_CATEGORIES
    ]
    reason = _text(decision.get("reason"))
    category = _failure_category(reason)
    if category is not None:
        diagnostics.append({"category": category, "reason": reason})
    return [dict(item) for item in sorted(diagnostics, key=_failure_key)]


def _skills(run_state: Mapping[str, object]) -> list[dict[str, object]]:
    skills = _records(run_state.get("skill_receipts")) or _records(run_state.get("skills"))
    return [dict(skill) for skill in sorted(skills, key=_skill_key)]


def _guidance(status: object, run_id: str | None, best_candidate: str | None) -> str:
    if status == "complete" and best_candidate is not None:
        return f"git merge {best_candidate}"
    if run_id is not None:
        return f"hoh resume --run-id {run_id}"
    return "hoh resume"


def _candidate(run_state: Mapping[str, object], name: str) -> str | None:
    return _text(run_state.get(name)) or _text(run_state.get(f"{name}_sha"))


def _evidence(loop: Mapping[str, object]) -> Mapping[str, object]:
    nested = loop.get("normalized_evidence")
    return _mapping(nested) or loop


def _records(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _claim_id(record: Mapping[str, object]) -> str:
    return _text(record.get("claim_id")) or ""


def _failure_key(record: Mapping[str, object]) -> tuple[str, str]:
    return (_text(record.get("category")) or "", _text(record.get("code")) or _text(record.get("reason")) or "")


def _skill_key(record: Mapping[str, object]) -> tuple[str, str]:
    return (_text(record.get("skill_id")) or "", _text(record.get("sha256")) or "")


def _failure_category(reason: str | None) -> str | None:
    if reason is None:
        return None
    for category in _FAILURE_CATEGORIES:
        if f"{category} failure" in reason:
            return category
    return None


def _bullet_values(value: object) -> list[str]:
    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else ()
    rendered = [f"- {_display(item)}" for item in sorted(item for item in values if isinstance(item, str))]
    return rendered or ["- none"]


def _display(value: object) -> str:
    if value is None:
        return "none"
    return str(value)
