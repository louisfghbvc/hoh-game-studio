"""Host-owned HoH loop orchestration and crash-safe phase resume."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from hoh.adapters.base import AdapterContext, ProductAdapter
from hoh.backends.base import (
    AgentBackend,
    AgentRequest,
    AgentResult,
    AgentUsage,
    BackendProcessError,
    BackendProtocolError,
    BackendTimeout,
)
from hoh.models import CheckBundle, HarnessConfig, Phase, Role, Sandbox
from hoh.policy import StopDecision, StopPolicy
from hoh.prompts import PromptRenderer
from hoh.reporting import build_status, render_run_summary, write_run_summary
from hoh.skills.registry import SkillDocument, SkillRegistry
from hoh.state.evidence import EvidenceBindingError, EvidenceNormalizer
from hoh.state.issue_ledger import IssueLedger, IssueLedgerError
from hoh.state.store import (
    RunLock,
    StateConflictError,
    StateError,
    StateStore,
    atomic_write_json,
)
from hoh.vcs.git import GitError, GitService, PreparedCommit, ProtectedPathError
from hoh.vcs.worktree import QaWorktree, QaWorktreeCleanupError


class OrchestratorError(RuntimeError):
    """Base error for host orchestration failures."""


class RoleOutputError(OrchestratorError):
    """Raised when a role returns data outside its host schema or loop contract."""


class RoleSchemaError(RoleOutputError):
    """Raised for the one repairable structured role-output failure class."""


class ResumeError(OrchestratorError):
    """Raised when no durable run can be resumed safely."""


class PreflightError(OrchestratorError):
    """Raised when a host or adapter prerequisite is blocked."""


_RETRYABLE_BACKEND_ERRORS = (BackendTimeout, BackendProcessError)
_REPAIRABLE_ROLE_ERRORS = (*_RETRYABLE_BACKEND_ERRORS, RoleSchemaError)
_PROTOCOL_ERRORS = (
    BackendProtocolError,
    EvidenceBindingError,
    IssueLedgerError,
    ProtectedPathError,
    RoleOutputError,
    StateConflictError,
)
_ROLE_SANDBOX = {
    Role.PLANNER: Sandbox.READ_ONLY,
    Role.DEVELOPER: Sandbox.WORKSPACE_WRITE,
    Role.QA: Sandbox.READ_ONLY,
}
_ROLE_SCHEMA = {
    Role.PLANNER: "plan.schema.json",
    Role.DEVELOPER: "developer-report.schema.json",
    Role.QA: "evidence.schema.json",
}
_DURABLE_RUN_STATUSES = frozenset(
    {"running", "resumable", "complete", "blocked", "budget_exhausted", "cancelled"}
)


class HoHOrchestrator:
    """Coordinate fresh role invocations around durable host-owned phases."""

    def __init__(
        self,
        config: HarnessConfig,
        backend: AgentBackend,
        adapter: ProductAdapter,
        git: GitService,
        store: StateStore,
        skills: SkillRegistry,
        policy: StopPolicy,
        *,
        prompt_renderer: PromptRenderer | None = None,
        schema_dir: Path | None = None,
        issue_ledger: IssueLedger | None = None,
        run_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        qa_worktree_factory: Callable[[GitService, Path], QaWorktree] | None = None,
        host_staging_root: Path | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.adapter = adapter
        self.git = git
        self.store = store
        self.skills = skills
        self.policy = policy
        self.prompt_renderer = prompt_renderer or PromptRenderer()
        self.schema_dir = (
            Path(schema_dir).resolve()
            if schema_dir is not None
            else (Path(__file__).parent / "resources" / "schemas").resolve()
        )
        self.issue_ledger = issue_ledger or IssueLedger(
            self.config.project / ".hoh" / "issue-ledger.json"
        )
        self._run_id_factory = run_id_factory or self._default_run_id
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._qa_worktree_factory = qa_worktree_factory or QaWorktree
        repository_key = hashlib.sha256(
            str(self.config.project.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        self._host_staging_root = (
            Path(host_staging_root).resolve()
            if host_staging_root is not None
            else (Path(tempfile.gettempdir()) / "hoh-host-events" / repository_key)
        )
        self._validate_composition()

    def run(self, max_loops: int | None = None) -> dict[str, object]:
        """Start a new run branch and execute bounded evidence-grounded loops."""

        loop_limit = self._loop_limit(max_loops)
        lock = RunLock(self.config.project / ".hoh" / "lock")
        lock.acquire()
        try:
            self.git.assert_product_clean()
            start_sha = self.git.head_sha()
            run_id = self._run_id_factory()
            branch = self.git.create_run_branch(run_id)
            run_dir = self.store.create_run(run_id, start_sha)
            run_state: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "start_sha": start_sha,
                "branch": branch,
                "status": "running",
                "started_at": self._now().isoformat(),
                "updated_at": self._now().isoformat(),
                "active_loop": 1,
                "loop_limit": loop_limit,
                "current_candidate": None,
                "best_candidate": None,
                "loops": [],
                "receipts": [],
                "skill_receipts": [],
            }
            self._write_run_state(run_dir, run_state)
            return self._drive_locked(run_dir, run_state)
        finally:
            lock.release()

    def resume(self, expected_run_id: str | None = None) -> dict[str, object]:
        """Continue the newest resumable run, optionally checking its identity.

        Selection and the optional identity check happen while holding the same
        product lock as execution, so callers cannot race a preflight lookup
        against the run that is actually resumed.
        """

        lock = RunLock(self.config.project / ".hoh" / "lock")
        lock.acquire()
        try:
            run_dir, run_state = self._latest_resumable_run()
            selected_run_id = _required_text(run_state, "run_id")
            if expected_run_id is not None and expected_run_id != selected_run_id:
                raise ResumeError(
                    "--run-id must match the newest resumable run; "
                    f"newest resumable run is {selected_run_id}"
                )
            branch = run_state.get("branch")
            if (
                not isinstance(branch, str)
                or self.git.current_branch_in(self.config.project) != branch
            ):
                raise ResumeError("the resumable run branch is not checked out")
            return self._drive_locked(run_dir, run_state)
        finally:
            lock.release()

    def inspect_latest(self) -> tuple[Path, dict[str, object]]:
        """Reconstruct the newest run status from validated durable artifacts.

        ``run.json`` is an aggregate/cache.  Inspection validates it against
        phase journals, hash-bound artifacts, receipts, the issue ledger, and
        Git commit identities before returning any user-facing status.
        """

        run_dir, run_state = self._latest_durable_run()
        return run_dir, self._inspect_run(run_dir, run_state)

    def _inspect_run(
        self, run_dir: Path, run_state: Mapping[str, object]
    ) -> dict[str, object]:
        run_id = _required_text(run_state, "run_id")
        if run_dir.name != run_id:
            raise StateConflictError("durable run metadata does not match its directory")
        start_sha = _required_text(run_state, "start_sha")
        try:
            if self.git.rev_parse_in(
                self.config.project, f"{start_sha}^{{commit}}"
            ) != start_sha:
                raise StateConflictError("durable run start commit is not canonical")
        except GitError as error:
            raise StateConflictError("durable run start commit cannot be resolved") from error
        expected_branch = f"hoh/run-{run_id}"
        if run_state.get("branch") != expected_branch:
            raise StateConflictError("durable run branch identity is malformed")

        loop_directories = self._durable_loop_directories(run_dir)
        previous: list[dict[str, object]] = []
        receipts: list[dict[str, object]] = []
        evidence_by_loop: list[tuple[int, Mapping[str, object]]] = []
        candidates: list[str] = []
        current_commit = start_sha
        last_decision: StopDecision | None = None
        last_evidence_hashes: Mapping[str, str] | None = None
        latest_qa_loop = 0
        allowed_heads: set[str] = {start_sha}

        for position, (loop_index, loop_dir) in enumerate(loop_directories):
            inspected = self._inspect_loop(
                run_id,
                loop_index,
                loop_dir,
                previous,
                expected_base_sha=current_commit,
            )
            receipts.extend(_mapping_records(inspected.get("receipts")))
            candidate = _optional_mapping(inspected.get("candidate"))
            if candidate:
                candidates.append(_required_text(candidate, "candidate_sha"))
            evidence = _optional_mapping(inspected.get("evidence"))
            if evidence:
                latest_qa_loop = loop_index
                evidence_by_loop.append((loop_index, evidence))
            loop_record = _optional_mapping(inspected.get("loop_record"))
            decision = inspected.get("decision")
            if loop_record:
                if not isinstance(decision, StopDecision):
                    raise StateConflictError("durable closure decision is missing")
                previous.append(dict(loop_record))
                current_commit = _required_text(loop_record, "evidence_commit_sha")
                last_decision = decision
                hashes = inspected.get("evidence_hashes")
                if not isinstance(hashes, Mapping):
                    raise StateConflictError("durable evidence intent hashes are missing")
                last_evidence_hashes = {
                    str(path): str(digest) for path, digest in hashes.items()
                }
                allowed_heads = {current_commit}
                if decision.should_stop and position != len(loop_directories) - 1:
                    raise StateConflictError("durable terminal closure has later loop state")
            else:
                if position != len(loop_directories) - 1:
                    raise StateConflictError("durable loop sequence has an incomplete gap")
                raw_heads = inspected.get("allowed_heads")
                if not isinstance(raw_heads, set) or not all(
                    isinstance(item, str) for item in raw_heads
                ):
                    raise StateConflictError("durable active loop Git identity is missing")
                allowed_heads = set(raw_heads)

        ledger = self.issue_ledger.validate_replay(evidence_by_loop)
        if (
            last_evidence_hashes is not None
            and latest_qa_loop == len(previous)
        ):
            ledger_relative = ".hoh/issue-ledger.json"
            expected_ledger_hash = last_evidence_hashes.get(ledger_relative)
            if (
                not isinstance(expected_ledger_hash, str)
                or _file_sha256(self.config.project / ledger_relative)
                != expected_ledger_hash
            ):
                raise StateConflictError(
                    "durable issue ledger does not match the latest evidence commit"
                )
        if (
            last_evidence_hashes is not None
            and last_decision is not None
            and last_decision.terminal_status == "complete"
        ):
            best_relative = ".hoh/best-candidate.json"
            expected_best_hash = last_evidence_hashes.get(best_relative)
            if (
                not isinstance(expected_best_hash, str)
                or _file_sha256(self.config.project / best_relative)
                != expected_best_hash
            ):
                raise StateConflictError(
                    "durable best candidate record does not match the completion commit"
                )

        try:
            head = self.git.head_sha()
        except GitError as error:
            raise StateConflictError("durable run Git HEAD cannot be resolved") from error
        if head not in allowed_heads:
            raise StateConflictError("Git HEAD does not match the durable run phase state")

        current_candidate = candidates[-1] if candidates else None
        terminal_decision = (
            last_decision
            if last_decision is not None and last_decision.should_stop
            else None
        )
        decision = self._inspection_decision(run_state, terminal_decision, run_id)
        best_candidate = (
            current_candidate if decision.terminal_status == "complete" else None
        )
        self._validate_run_aggregate(
            run_state,
            previous,
            receipts,
            candidates,
            best_candidate,
            decision,
        )
        reconstructed: dict[str, object] = {
            "run_id": run_id,
            "start_sha": start_sha,
            "loops": previous,
            "receipts": receipts,
            "skill_receipts": _unique_skill_receipts(receipts),
            "current_candidate": current_candidate,
            "best_candidate": best_candidate,
            "elapsed_seconds": _nonnegative_int(run_state.get("elapsed_seconds")),
        }
        status = build_status(
            reconstructed,
            receipts=receipts,
            decision=decision,
            issue_ledger=ledger,
        )
        result = dict(status)
        result["status"] = status["terminal_status"]
        result["loops_completed"] = status["completed_loops"]
        return result

    def _inspect_loop(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        previous: list[dict[str, object]],
        *,
        expected_base_sha: str,
    ) -> dict[str, object]:
        payloads: dict[Phase, dict[str, object]] = {}
        found_gap = False
        for phase in Phase:
            payload = self._phase_payload(run_id, loop_index, phase)
            if payload is None:
                found_gap = True
                continue
            if found_gap:
                raise StateConflictError("durable phase journal is not a contiguous prefix")
            payloads[phase] = payload

        preflight = payloads.get(Phase.PREFLIGHT)
        if preflight is not None:
            diagnostics = preflight.get("diagnostics")
            if preflight.get("adapter") != self.config.adapter or not isinstance(
                diagnostics, list
            ) or any(not isinstance(item, Mapping) for item in diagnostics):
                raise StateConflictError("durable preflight phase is malformed")

        plan = (
            self._read_descriptor(payloads[Phase.PLANNING], "plan")
            if Phase.PLANNING in payloads
            else None
        )
        baseline = (
            self._read_descriptor(payloads[Phase.BASELINE], "checks")
            if Phase.BASELINE in payloads
            else None
        )
        development: dict[str, object] | None = None
        if Phase.DEVELOPMENT in payloads:
            payload = payloads[Phase.DEVELOPMENT]
            changed_paths = list(_string_sequence(payload.get("changed_paths")))
            base_sha = _required_text(payload, "base_sha")
            if base_sha != expected_base_sha:
                raise StateConflictError("durable development has a wrong Git base")
            mutation_manifest = self._read_descriptor(payload, "mutation_manifest")
            self._validate_product_mutation_manifest(
                mutation_manifest,
                expected_base_sha=base_sha,
                expected_paths=changed_paths,
            )
            development = {
                "report": self._read_descriptor(payload, "report"),
                "base_sha": base_sha,
                "changed_paths": changed_paths,
                "mutation_manifest": mutation_manifest,
            }

        candidate: dict[str, object] | None = None
        if Phase.CANDIDATE in payloads:
            if development is None:
                raise StateConflictError("durable candidate has no development phase")
            candidate = self._read_descriptor(payloads[Phase.CANDIDATE], "candidate")
            self._validate_candidate(candidate)
            candidate_sha = _required_text(candidate, "candidate_sha")
            if not self._is_direct_child(candidate_sha, expected_base_sha):
                raise StateConflictError("durable candidate has a wrong Git parent")
            self._validate_candidate_intent(loop_index, loop_dir, development, candidate)

        pending_candidate: PreparedCommit | None = None
        if (
            development is not None
            and candidate is None
            and (loop_dir / "candidate-commit-intent.json").is_file()
        ):
            pending_candidate = self._validate_candidate_intent(
                loop_index, loop_dir, development, None
            )

        checks: dict[str, object] | None = None
        manifest: dict[str, object] | None = None
        if Phase.CHECKING in payloads:
            if candidate is None:
                raise StateConflictError("durable checks have no candidate phase")
            payload = payloads[Phase.CHECKING]
            checks = self._read_descriptor(payload, "checks")
            manifest = self._read_descriptor(payload, "manifest")
            self._require_candidate_binding(checks, candidate)
            self._require_candidate_binding(manifest, candidate)
            if manifest.get("deterministic_check_id") != _required_text(
                checks, "check_id"
            ):
                raise StateConflictError(
                    "durable checks and adapter manifest have different identities"
                )
            self._validated_manifest_artifact_paths(loop_dir, manifest)

        qa: dict[str, object] | None = None
        evidence: dict[str, object] | None = None
        if Phase.QA in payloads:
            if candidate is None or manifest is None:
                raise StateConflictError("durable QA has no candidate checks")
            payload = payloads[Phase.QA]
            response_descriptor = _required_mapping(payload, "response")
            invocation_id = _required_text(payload, "qa_invocation_id")
            self._validate_qa_invocation(
                run_id,
                loop_index,
                loop_dir,
                invocation_id,
                response_descriptor,
            )
            response = self._read_descriptor(payload, "response")
            evidence = self._read_descriptor(payload, "evidence")
            self._require_candidate_binding(evidence, candidate)
            self._revalidate_qa_response(
                loop_index, loop_dir, response, evidence, candidate, manifest
            )
            release_gate: dict[str, object] = {}
            release_gate_path = loop_dir / "release-gate.json"
            if release_gate_path.is_file():
                try:
                    release_gate = _read_json(release_gate_path)
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    StateError,
                ) as error:
                    raise StateConflictError("durable release gate is malformed") from error
                self._validate_release_gate(loop_dir, release_gate, candidate)
            qa = {
                "evidence": evidence,
                "qa_invocation_id": invocation_id,
                "release_gate": release_gate,
            }

        receipts = self._attempt_receipts(run_id, loop_index, loop_dir)
        loop_record: dict[str, object] | None = None
        decision: StopDecision | None = None
        evidence_hashes: Mapping[str, str] | None = None
        pending_evidence_sha: str | None = None
        if Phase.CLOSURE in payloads:
            if development is None or candidate is None or checks is None or qa is None:
                raise StateConflictError("durable closure is missing preceding phases")
            payload = payloads[Phase.CLOSURE]
            loop_record = self._read_descriptor(payload, "loop_record")
            receipt = self._read_descriptor(payload, "receipt")
            expected_receipt = {
                "schema_version": 1,
                "run_id": run_id,
                "loop_index": loop_index,
                "candidate_sha": _required_text(candidate, "candidate_sha"),
                "attempts": receipts,
            }
            if receipt != expected_receipt:
                raise StateConflictError("durable closure receipt conflicts with attempts")
            self._validate_completed_closure(
                payload,
                loop_record,
                previous,
                development,
                candidate,
                checks,
                qa,
                require_head=False,
            )
            decision = self._closure_decision(payload)
            evidence_hashes = self._validate_evidence_intent(
                run_id,
                loop_index,
                loop_dir,
                candidate,
                manifest,
                qa,
                payload,
                include_best=decision.terminal_status == "complete",
            )
        elif (
            qa is not None
            and candidate is not None
            and manifest is not None
            and (loop_dir / "evidence-commit-intent.json").is_file()
        ):
            pending_evidence_sha = self._validate_pending_evidence_intent(
                run_id, loop_index, loop_dir, candidate, manifest, qa
            )

        allowed_heads = {expected_base_sha}
        if pending_candidate is not None:
            allowed_heads.add(pending_candidate.commit_sha)
        if candidate is not None:
            allowed_heads = {_required_text(candidate, "candidate_sha")}
            if pending_evidence_sha is not None:
                allowed_heads.add(pending_evidence_sha)
        if loop_record is not None:
            allowed_heads = {_required_text(loop_record, "evidence_commit_sha")}
        return {
            "candidate": candidate or {},
            "evidence": evidence or {},
            "loop_record": loop_record or {},
            "decision": decision,
            "receipts": receipts,
            "evidence_hashes": evidence_hashes,
            "allowed_heads": allowed_heads,
        }

    def _drive_locked(
        self, run_dir: Path, run_state: dict[str, object]
    ) -> dict[str, object]:
        """Drive one run while its caller owns the product lock."""

        started = self._monotonic()
        persisted = run_state.get("elapsed_seconds", 0)
        persisted_seconds = (
            persisted
            if isinstance(persisted, int)
            and not isinstance(persisted, bool)
            and persisted >= 0
            else 0
        )
        elapsed_base = max(
            persisted_seconds, self._elapsed_from_timestamp(run_state)
        )
        try:
            while True:
                loop_index = _required_positive_int(run_state, "active_loop")
                loop_record, decision = self._execute_loop(
                    run_dir, run_state, loop_index, started, elapsed_base
                )
                elapsed_seconds = self._elapsed_seconds(
                    run_state, started, elapsed_base
                )
                self._record_closed_loop(
                    run_dir,
                    run_state,
                    loop_record,
                    decision,
                    elapsed_seconds,
                )
                if decision.should_stop:
                    return self._terminal_result(run_dir, run_state, decision)
                run_state["active_loop"] = loop_index + 1
                run_state["updated_at"] = self._now().isoformat()
                self._write_run_state(run_dir, run_state)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return self._persist_cancellation(
                run_dir,
                run_state,
                self._elapsed_seconds(run_state, started, elapsed_base),
            )
        except BaseException as error:
            self._persist_failure(run_dir, run_state, error)
            raise

    def _execute_loop(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        loop_index: int,
        started: float,
        elapsed_base: int,
    ) -> tuple[dict[str, object], StopDecision]:
        run_id = _required_text(run_state, "run_id")
        loop_dir = run_dir / "loops" / f"loop-{loop_index:04d}"
        loop_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_preflight(run_id, loop_index)
        previous = _mapping_records(run_state.get("loops"))
        plan = self._ensure_plan(run_id, loop_index, loop_dir, previous)
        baseline = self._ensure_baseline(run_id, loop_index, loop_dir, plan)
        development = self._ensure_development(
            run_id, loop_index, loop_dir, previous, plan, baseline
        )
        candidate = self._ensure_candidate(
            run_id, loop_index, loop_dir, development
        )
        checks, manifest = self._ensure_checks(
            run_id, loop_index, loop_dir, plan, candidate
        )
        qa = self._ensure_qa(
            run_id,
            loop_index,
            loop_dir,
            previous,
            plan,
            candidate,
            checks,
            manifest,
        )
        return self._ensure_closure(
            run_id,
            loop_index,
            loop_dir,
            run_state,
            previous,
            plan,
            development,
            candidate,
            checks,
            manifest,
            qa,
            started,
            elapsed_base,
        )

    def _ensure_preflight(self, run_id: str, loop_index: int) -> None:
        payload = self._phase_payload(run_id, loop_index, Phase.PREFLIGHT)
        if payload is not None:
            return
        diagnostics = self.adapter.doctor(self.config.project)
        public_diagnostics = [asdict(diagnostic) for diagnostic in diagnostics]
        blockers = [item for item in public_diagnostics if item.get("severity") == "blocked"]
        if blockers:
            messages = "; ".join(str(item.get("message")) for item in blockers)
            raise PreflightError(messages or "adapter prerequisite is blocked")
        self.store.complete_phase(
            run_id,
            loop_index,
            Phase.PREFLIGHT,
            {"adapter": self.config.adapter, "diagnostics": public_diagnostics},
        )

    def _ensure_plan(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        previous: list[dict[str, object]],
    ) -> dict[str, object]:
        payload = self._phase_payload(run_id, loop_index, Phase.PLANNING)
        if payload is not None:
            return self._read_descriptor(payload, "plan")
        skills = self.skills.select(
            Role.PLANNER, self.config.adapter, ("core.bounded-planning",)
        )
        prompt = self.prompt_renderer.render(
            Role.PLANNER,
            self._prompt_context(
                workspace=self.config.project,
                previous=previous,
                current_plan={},
                checks=[],
            ),
            skills,
        )
        plan, _ = self._invoke_role(
            run_id,
            loop_index,
            loop_dir,
            Role.PLANNER,
            prompt,
            self.config.project,
            skills,
            invocation_kind="ordinary",
            protected_paths=self.config.protected_paths,
            read_only_workspace=self.config.project,
            response_validator=lambda response: self._validate_role_semantics(
                Role.PLANNER, response, loop_index
            ),
        )
        plan_path = loop_dir / "plan.json"
        atomic_write_json(plan_path, plan)
        descriptor = self._descriptor(plan_path)
        self.store.complete_phase(
            run_id, loop_index, Phase.PLANNING, {"plan": descriptor}
        )
        return plan

    def _ensure_baseline(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        plan: dict[str, object],
    ) -> dict[str, object]:
        payload = self._phase_payload(run_id, loop_index, Phase.BASELINE)
        if payload is not None:
            return self._read_descriptor(payload, "checks")
        staged = self._host_staging_root / run_id / f"loop-{loop_index:04d}" / "baseline"
        if staged.exists():
            shutil.rmtree(staged)
        bundle = self.adapter.baseline(AdapterContext(self.config.project, staged), plan)
        public = _bundle_document(bundle)
        baseline_path = loop_dir / "baseline-checks.json"
        atomic_write_json(baseline_path, public)
        self.store.complete_phase(
            run_id,
            loop_index,
            Phase.BASELINE,
            {"checks": self._descriptor(baseline_path)},
        )
        return public

    def _ensure_development(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        previous: list[dict[str, object]],
        plan: dict[str, object],
        baseline: dict[str, object],
    ) -> dict[str, object]:
        payload = self._phase_payload(run_id, loop_index, Phase.DEVELOPMENT)
        if payload is not None:
            changed_paths = list(_string_sequence(payload.get("changed_paths")))
            base_sha = _required_text(payload, "base_sha")
            mutation_manifest = self._read_descriptor(
                payload, "mutation_manifest"
            )
            self._validate_product_mutation_manifest(
                mutation_manifest,
                expected_base_sha=base_sha,
                expected_paths=changed_paths,
            )
            return {
                "report": self._read_descriptor(payload, "report"),
                "base_sha": base_sha,
                "changed_paths": changed_paths,
                "mutation_manifest": mutation_manifest,
            }
        skills = self._developer_skills(plan)
        prompt = self.prompt_renderer.render(
            Role.DEVELOPER,
            self._prompt_context(
                workspace=self.config.project,
                previous=previous,
                current_plan=plan,
                checks=baseline,
            ),
            skills,
        )
        base_sha = self.git.head_sha()
        report, _ = self._invoke_role(
            run_id,
            loop_index,
            loop_dir,
            Role.DEVELOPER,
            prompt,
            self.config.project,
            skills,
            invocation_kind="ordinary",
            protected_paths=self.config.protected_paths,
            response_validator=lambda response: self._validate_role_semantics(
                Role.DEVELOPER, response, loop_index
            ),
        )
        changed_paths = self._production_changed_paths(base_sha)
        mutation_manifest = self._product_mutation_manifest(
            base_sha, changed_paths
        )
        report_path = loop_dir / "developer-report.json"
        mutation_manifest_path = loop_dir / "product-mutation.json"
        atomic_write_json(report_path, report)
        atomic_write_json(mutation_manifest_path, mutation_manifest)
        phase_payload = {
            "report": self._descriptor(report_path),
            "base_sha": base_sha,
            "changed_paths": list(changed_paths),
            "mutation_manifest": self._descriptor(mutation_manifest_path),
        }
        self.store.complete_phase(
            run_id, loop_index, Phase.DEVELOPMENT, phase_payload
        )
        return {
            "report": report,
            "base_sha": base_sha,
            "changed_paths": list(changed_paths),
            "mutation_manifest": mutation_manifest,
        }

    def _ensure_candidate(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        development: dict[str, object],
    ) -> dict[str, object]:
        payload = self._phase_payload(run_id, loop_index, Phase.CANDIDATE)
        if payload is not None:
            candidate = self._read_descriptor(payload, "candidate")
            self._validate_candidate(candidate)
            return candidate
        report = _required_mapping(development, "report")
        summary = _required_text(report, "summary")
        base_sha = _required_text(development, "base_sha")
        changed_paths = list(_string_sequence(development.get("changed_paths")))
        mutation_manifest = _required_mapping(development, "mutation_manifest")
        mutation_hashes = self._assert_product_mutation_matches(
            base_sha,
            changed_paths,
            mutation_manifest,
        )
        intent_path = loop_dir / "candidate-commit-intent.json"
        prepared = self._load_commit_intent(
            intent_path,
            expected_kind="candidate",
            expected_loop_index=loop_index,
            expected_parent_sha=base_sha,
            expected_selected_paths=changed_paths,
            expected_selected_hashes=mutation_hashes,
        )
        if prepared is None:
            if self.git.head_sha() != base_sha:
                raise StateConflictError(
                    "Git HEAD moved before candidate commit intent was durable"
                )
            try:
                prepared = self.git.prepare_candidate(
                    loop_index,
                    summary,
                    tuple(changed_paths),
                    mutation_manifest=mutation_manifest,
                )
            except GitError as error:
                raise StateConflictError(
                    "candidate repository mutations do not match the durable manifest"
                ) from error
            if prepared.parent_sha != base_sha:
                raise StateConflictError(
                    "prepared candidate has a different expected parent"
                )
            self._write_commit_intent(
                intent_path,
                prepared,
                changed_paths,
                mutation_hashes,
            )
        try:
            candidate_sha = self.git.land_prepared_commit(prepared)
        except GitError as error:
            raise StateConflictError(
                "Git HEAD does not match the durable prepared candidate intent"
            ) from error
        if self._production_changed_paths(candidate_sha):
            raise StateConflictError(
                "working production files do not match the prepared candidate"
            )
        tree_id = self.git.rev_parse_in(
            self.config.project, f"{candidate_sha}^{{tree}}"
        )
        if tree_id != prepared.tree_sha:
            raise StateConflictError("prepared candidate tree identity changed")
        candidate = {
            "schema_version": 1,
            "run_id": run_id,
            "loop_index": loop_index,
            "candidate_sha": candidate_sha,
            "artifact_tree_sha256": hashlib.sha256(tree_id.encode("ascii")).hexdigest(),
            "changed_paths": changed_paths,
        }
        candidate_path = loop_dir / "candidate.json"
        atomic_write_json(candidate_path, candidate)
        self.store.complete_phase(
            run_id,
            loop_index,
            Phase.CANDIDATE,
            {"candidate": self._descriptor(candidate_path)},
        )
        return candidate

    def _ensure_checks(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        plan: dict[str, object],
        candidate: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        payload = self._phase_payload(run_id, loop_index, Phase.CHECKING)
        if payload is not None:
            checks = self._read_descriptor(payload, "checks")
            manifest = self._read_descriptor(payload, "manifest")
            self._require_candidate_binding(checks, candidate)
            self._require_candidate_binding(manifest, candidate)
            check_id = _required_text(checks, "check_id")
            if manifest.get("deterministic_check_id") != check_id:
                raise StateConflictError(
                    "durable checks and adapter manifest have different identities"
                )
            return checks, manifest
        candidate_sha = _required_text(candidate, "candidate_sha")
        output = loop_dir / "adapter"
        check_id = f"{run_id}:loop-{loop_index:04d}:candidate-check"

        def check(frozen: Path) -> tuple[CheckBundle, dict[str, str]]:
            bundle = self.adapter.check(AdapterContext(frozen, output), plan)
            return bundle, self.adapter.collect(AdapterContext(frozen, output), bundle)

        bundle, collected = self._with_frozen_candidate(
            run_id, loop_index, candidate_sha, check
        )
        checks = {
            **_bundle_document(bundle),
            "check_id": check_id,
            "candidate_sha": candidate_sha,
            "artifact_tree_sha256": _required_text(
                candidate, "artifact_tree_sha256"
            ),
        }
        artifacts = self._adapter_manifest_artifacts(loop_dir, output, bundle, collected)
        manifest: dict[str, object] = {
            "schema_version": 1,
            "adapter": bundle.adapter,
            "status": bundle.status,
            "candidate_sha": candidate_sha,
            "artifact_tree_sha256": _required_text(candidate, "artifact_tree_sha256"),
            "deterministic_check_id": check_id,
            "artifacts": artifacts,
            "diagnostics": [],
        }
        checks_path = loop_dir / "checks.json"
        manifest_path = loop_dir / "adapter-manifest.json"
        atomic_write_json(checks_path, checks)
        atomic_write_json(manifest_path, manifest)
        self.store.complete_phase(
            run_id,
            loop_index,
            Phase.CHECKING,
            {
                "checks": self._descriptor(checks_path),
                "manifest": self._descriptor(manifest_path),
            },
        )
        return checks, manifest

    def _ensure_qa(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        previous: list[dict[str, object]],
        plan: dict[str, object],
        candidate: dict[str, object],
        checks: dict[str, object],
        manifest: dict[str, object],
    ) -> dict[str, object]:
        payload = self._phase_payload(run_id, loop_index, Phase.QA)
        if payload is not None:
            response_descriptor = _required_mapping(payload, "response")
            invocation_id = _required_text(payload, "qa_invocation_id")
            self._validate_qa_invocation(
                run_id,
                loop_index,
                loop_dir,
                invocation_id,
                response_descriptor,
            )
            response = self._read_descriptor(payload, "response")
            evidence = self._read_descriptor(payload, "evidence")
            self._require_candidate_binding(evidence, candidate)
            self._revalidate_qa_response(
                loop_index, loop_dir, response, evidence, candidate, manifest
            )
            self.issue_ledger.apply(evidence, loop_index)
            release_gate = self._ensure_release_gate(
                run_id,
                loop_index,
                loop_dir,
                previous,
                plan,
                candidate,
                evidence,
                manifest,
            )
            return {
                "evidence": evidence,
                "qa_invocation_id": invocation_id,
                "release_gate": release_gate,
            }
        candidate_sha = _required_text(candidate, "candidate_sha")
        skills = self._qa_skills()

        def qa(frozen: Path) -> tuple[dict[str, object], dict[str, object]]:
            context_checks = {
                "scope": "candidate",
                "candidate_sha": candidate_sha,
                "artifact_tree_sha256": _required_text(
                    candidate, "artifact_tree_sha256"
                ),
                "checks": checks,
                "adapter_manifest": manifest,
            }
            prompt = self.prompt_renderer.render(
                Role.QA,
                self._prompt_context(
                    workspace=frozen,
                    previous=previous,
                    current_plan=plan,
                    checks=context_checks,
                ),
                skills,
            )
            raw, receipt = self._invoke_role(
                run_id,
                loop_index,
                loop_dir,
                Role.QA,
                prompt,
                frozen,
                skills,
                invocation_kind="ordinary",
                read_only_workspace=frozen,
                response_validator=lambda response: self._validate_role_semantics(
                    Role.QA, response, loop_index
                ),
            )
            return raw, receipt

        raw, qa_receipt = self._with_frozen_candidate(
            run_id, loop_index, candidate_sha, qa
        )
        invocation_id = _required_text(qa_receipt, "invocation_id")
        response_descriptor = _required_mapping(qa_receipt, "response")
        normalizer = EvidenceNormalizer(
            loop_dir, self.config.project / ".hoh" / "requirements.json"
        )
        normalized = normalizer.normalize(
            raw,
            candidate_sha,
            _required_text(candidate, "artifact_tree_sha256"),
            manifest,
        )
        response_path = loop_dir / "qa-response.json"
        evidence_path = loop_dir / "evidence.json"
        if response_descriptor != self._descriptor(response_path):
            raise StateConflictError(
                "successful QA receipt has a different response descriptor"
            )
        atomic_write_json(evidence_path, normalized)
        phase_payload: dict[str, object] = {
            "response": response_descriptor,
            "evidence": self._descriptor(evidence_path),
            "qa_invocation_id": invocation_id,
        }
        self.store.complete_phase(run_id, loop_index, Phase.QA, phase_payload)
        self._validate_qa_invocation(
            run_id,
            loop_index,
            loop_dir,
            invocation_id,
            response_descriptor,
        )
        self.issue_ledger.apply(normalized, loop_index)
        release_gate = self._ensure_release_gate(
            run_id,
            loop_index,
            loop_dir,
            previous,
            plan,
            candidate,
            normalized,
            manifest,
        )
        return {
            "evidence": normalized,
            "qa_invocation_id": invocation_id,
            "release_gate": release_gate,
        }

    def _validate_qa_invocation(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        invocation_id: str,
        response_descriptor: Mapping[str, object],
        *,
        invocation_kind: str = "ordinary",
        context_descriptor: Mapping[str, object] | None = None,
    ) -> None:
        successful = [
            receipt
            for receipt in self._validated_role_attempts(
                run_id, loop_index, loop_dir, Role.QA, invocation_kind
            )
            if receipt.get("outcome") == "success"
        ]
        durable_descriptor = (
            successful[0].get("response") if len(successful) == 1 else None
        )
        durable_context = (
            successful[0].get("context") if len(successful) == 1 else None
        )
        if (
            len(successful) != 1
            or successful[0].get("invocation_id") != invocation_id
            or not isinstance(durable_descriptor, Mapping)
            or dict(durable_descriptor) != dict(response_descriptor)
            or (
                invocation_kind == "full-release"
                and (
                    not isinstance(durable_context, Mapping)
                    or context_descriptor is None
                    or dict(durable_context) != dict(context_descriptor)
                )
            )
            or (
                invocation_kind != "full-release"
                and (durable_context is not None or context_descriptor is not None)
            )
        ):
            raise StateConflictError(
                "durable QA phase has a different successful invocation identity "
                "or response/context descriptor"
            )

    def _ensure_release_gate(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        previous: list[dict[str, object]],
        plan: dict[str, object],
        candidate: dict[str, object],
        ordinary_evidence: dict[str, object],
        manifest: dict[str, object],
    ) -> dict[str, object]:
        """Load or produce the post-QA release gate as a separate durable record."""

        gate_path = loop_dir / "release-gate.json"
        if gate_path.is_file():
            try:
                gate = _read_json(gate_path)
            except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
                raise StateConflictError("durable release gate is malformed") from error
            self._validate_release_gate(loop_dir, gate, candidate)
            return gate
        if not self._ordinary_candidate_is_release_ready(ordinary_evidence, manifest):
            return {}
        gate = self._run_release_gate(
            run_id,
            loop_index,
            loop_dir,
            previous,
            plan,
            candidate,
            ordinary_evidence,
            self._qa_skills(),
        )
        atomic_write_json(gate_path, gate)
        self._validate_release_gate(loop_dir, gate, candidate)
        return gate

    def _ordinary_candidate_is_release_ready(
        self,
        evidence: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> bool:
        if (
            evidence.get("product_complete") is not True
            or evidence.get("qa_status") != "pass"
            or manifest.get("status") != "pass"
        ):
            return False
        ledger = self.issue_ledger.load()
        for issue in _mapping_records(ledger.get("issues")):
            if issue.get("status") in {"open", "regressed"} and issue.get(
                "severity"
            ) in {"blocker", "major"}:
                return False
        return True

    def _run_release_gate(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        previous: list[dict[str, object]],
        plan: dict[str, object],
        candidate: dict[str, object],
        ordinary_evidence: dict[str, object],
        skills: tuple[SkillDocument, ...],
    ) -> dict[str, object]:
        candidate_sha = _required_text(candidate, "candidate_sha")
        artifact_tree_sha256 = _required_text(candidate, "artifact_tree_sha256")
        check_id = f"{run_id}:loop-{loop_index:04d}:full-release-check"
        output = loop_dir / "release-adapter"

        successful = [
            receipt
            for receipt in self._validated_role_attempts(
                run_id, loop_index, loop_dir, Role.QA, "full-release"
            )
            if receipt.get("outcome") == "success"
        ]
        if successful:
            qa_receipt = successful[0]
            context_descriptor = _required_mapping(qa_receipt, "context")
            context, checks, manifest, _ = self._validated_release_invocation_context(
                run_id,
                loop_index,
                loop_dir,
                context_descriptor,
                candidate,
            )
            raw = self._validated_successful_qa_response(
                loop_index, loop_dir, qa_receipt, "full-release"
            )
        else:

            def release(
                frozen: Path,
            ) -> tuple[dict[str, object], dict[str, object]]:
                bundle = self.adapter.check(AdapterContext(frozen, output), plan)
                collected = self.adapter.collect(
                    AdapterContext(frozen, output), bundle
                )
                fresh_checks: dict[str, object] = {
                    "schema_version": 1,
                    "check_id": check_id,
                    "scope": "full_release",
                    "candidate_sha": candidate_sha,
                    "artifact_tree_sha256": artifact_tree_sha256,
                    "bundle": _bundle_document(bundle),
                }
                artifacts = self._adapter_manifest_artifacts(
                    loop_dir, output, bundle, collected
                )
                fresh_manifest: dict[str, object] = {
                    "schema_version": 1,
                    "adapter": bundle.adapter,
                    "status": bundle.status,
                    "candidate_sha": candidate_sha,
                    "artifact_tree_sha256": artifact_tree_sha256,
                    "deterministic_check_id": check_id,
                    "scope": "full_release",
                    "artifacts": artifacts,
                    "diagnostics": [],
                }
                checks_path = loop_dir / "release-checks.json"
                manifest_path = loop_dir / "release-adapter-manifest.json"
                atomic_write_json(checks_path, fresh_checks)
                atomic_write_json(manifest_path, fresh_manifest)
                fresh_context_descriptor = (
                    self._persist_release_invocation_context(
                        run_id,
                        loop_index,
                        loop_dir,
                        candidate,
                        check_id,
                    )
                )
                context_checks = {
                    "scope": "full_release",
                    "candidate_sha": candidate_sha,
                    "artifact_tree_sha256": artifact_tree_sha256,
                    "deterministic_check_id": check_id,
                    "ordinary_evidence": ordinary_evidence,
                    "checks": fresh_checks,
                    "adapter_manifest": fresh_manifest,
                }
                prompt = (
                    "# FULL RELEASE QA / E2E GATE\n\n"
                    "This is a distinct fresh full-release invocation against the same "
                    "frozen candidate. Re-run independent end-to-end acceptance from "
                    "the retained full-release check records; do not reuse the ordinary "
                    "loop verdict.\n\n"
                    + self.prompt_renderer.render(
                        Role.QA,
                        self._prompt_context(
                            workspace=frozen,
                            previous=previous,
                            current_plan=plan,
                            checks=context_checks,
                        ),
                        skills,
                    )
                )
                return self._invoke_role(
                    run_id,
                    loop_index,
                    loop_dir,
                    Role.QA,
                    prompt,
                    frozen,
                    skills,
                    invocation_kind="full-release",
                    read_only_workspace=frozen,
                    response_validator=lambda response: self._validate_role_semantics(
                        Role.QA, response, loop_index
                    ),
                    invocation_context_descriptor=fresh_context_descriptor,
                )

            raw, qa_receipt = self._with_frozen_candidate(
                run_id, loop_index, candidate_sha, release
            )
            context_descriptor = _required_mapping(qa_receipt, "context")
            context, checks, manifest, _ = self._validated_release_invocation_context(
                run_id,
                loop_index,
                loop_dir,
                context_descriptor,
                candidate,
            )

        invocation_id = _required_text(qa_receipt, "invocation_id")
        response_descriptor = _required_mapping(qa_receipt, "response")
        normalizer = EvidenceNormalizer(
            loop_dir, self.config.project / ".hoh" / "requirements.json"
        )
        normalized = normalizer.normalize(
            raw,
            candidate_sha,
            artifact_tree_sha256,
            manifest,
        )
        release_response_path = loop_dir / "release-qa-response.json"
        release_evidence_path = loop_dir / "release-evidence.json"
        if response_descriptor != self._descriptor(release_response_path):
            raise StateConflictError(
                "successful full-release QA receipt has a different response descriptor"
            )
        atomic_write_json(release_evidence_path, normalized)
        checks_bundle = _required_mapping(checks, "bundle")
        deterministic_passed = checks_bundle.get("status") == "pass"
        release_qa_passed = (
            normalized.get("qa_status") == "pass"
            and normalized.get("product_complete") is True
        )
        return {
            "invocation_id": invocation_id,
            "candidate_sha": candidate_sha,
            "artifact_tree_sha256": artifact_tree_sha256,
            "scope": "full_release",
            "qa_status": "pass" if release_qa_passed else "fail",
            "end_to_end_passed": deterministic_passed,
            "deterministic_checks_passed": deterministic_passed,
            "check_id": check_id,
            "deterministic_checks_candidate_sha": candidate_sha,
            "checks": _required_mapping(context, "checks"),
            "manifest": _required_mapping(context, "manifest"),
            "context": dict(context_descriptor),
            "response": response_descriptor,
            "evidence": self._descriptor(release_evidence_path),
        }

    def _persist_release_invocation_context(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        candidate: Mapping[str, object],
        check_id: str,
    ) -> dict[str, str]:
        """Persist the exact release inputs before a successful receipt can exist."""

        checks_path = loop_dir / "release-checks.json"
        manifest_path = loop_dir / "release-adapter-manifest.json"
        evidence_path = loop_dir / "evidence.json"
        try:
            checks = _read_json(
                self._required_regular_evidence_path(checks_path, loop_dir)
            )
            manifest = _read_json(
                self._required_regular_evidence_path(manifest_path, loop_dir)
            )
            self._required_regular_evidence_path(evidence_path, loop_dir)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            if isinstance(error, StateConflictError):
                raise
            raise StateConflictError(
                "full-release invocation context inputs are invalid"
            ) from error
        context: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "loop_index": loop_index,
            "scope": "full_release",
            "candidate_sha": _required_text(candidate, "candidate_sha"),
            "artifact_tree_sha256": _required_text(
                candidate, "artifact_tree_sha256"
            ),
            "check_id": check_id,
            "ordinary_evidence": self._descriptor(evidence_path),
            "checks": self._descriptor(checks_path),
            "manifest": self._descriptor(manifest_path),
            "artifacts": _required_mapping(manifest, "artifacts"),
        }
        identity = _canonical_json_sha256(context)
        context_path = loop_dir / f"release-context-{identity[:32]}.json"
        if context_path.exists() or context_path.is_symlink():
            try:
                retained_path = self._required_regular_evidence_path(
                    context_path, loop_dir
                )
                retained = _read_json(retained_path)
            except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
                raise StateConflictError(
                    "durable release invocation context is malformed"
                ) from error
            if retained != context:
                raise StateConflictError(
                    "durable release invocation context is immutable"
                )
        else:
            atomic_write_json(context_path, context)
        descriptor = self._descriptor(context_path)
        self._validated_release_invocation_context(
            run_id, loop_index, loop_dir, descriptor, candidate
        )
        return descriptor

    def _validated_release_invocation_context(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        descriptor: Mapping[str, object],
        candidate: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object], Path]:
        """Validate a success receipt's immutable release inputs without rerunning them."""

        loaded_candidate = candidate is None
        if candidate is None:
            try:
                candidate_path = self._required_regular_evidence_path(
                    loop_dir / "candidate.json", loop_dir
                )
                candidate = _read_json(candidate_path)
            except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
                raise StateConflictError(
                    "durable release invocation candidate is invalid"
                ) from error
        candidate_loop_index = candidate.get("loop_index")
        if (
            candidate.get("run_id") != run_id
            or isinstance(candidate_loop_index, bool)
            or not isinstance(candidate_loop_index, int)
            or candidate_loop_index != loop_index
        ):
            raise StateConflictError(
                "durable release invocation candidate identity is malformed"
            )
        if loaded_candidate:
            try:
                self._validate_candidate(candidate)
            except (GitError, StateError) as error:
                raise StateConflictError(
                    "durable release invocation candidate does not match Git"
                ) from error

        if set(descriptor) != {"path", "sha256"}:
            raise StateConflictError(
                "durable release invocation context descriptor is malformed"
            )
        relative = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        ):
            raise StateConflictError(
                "durable release invocation context descriptor is malformed"
            )
        filename_match = re.fullmatch(
            r"release-context-([0-9a-f]{32})\.json",
            PurePosixPath(relative).name,
        )
        if filename_match is None:
            raise StateConflictError(
                "durable release invocation context path is malformed"
            )
        context_path = loop_dir / PurePosixPath(relative).name
        try:
            retained_path = self._required_regular_evidence_path(
                context_path, loop_dir
            )
            expected_relative = self._project_relative(retained_path)
            if relative != expected_relative:
                raise StateConflictError(
                    "durable release invocation context path is not canonical"
                )
            if _file_sha256(retained_path) != expected_hash:
                raise StateConflictError(
                    "durable release invocation context hash does not match"
                )
            context = _read_json(retained_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            raise StateConflictError(
                "durable release invocation context artifact is invalid"
            ) from error
        allowed = {
            "schema_version",
            "run_id",
            "loop_index",
            "scope",
            "candidate_sha",
            "artifact_tree_sha256",
            "check_id",
            "ordinary_evidence",
            "checks",
            "manifest",
            "artifacts",
        }
        if (
            set(context) != allowed
            or isinstance(context.get("schema_version"), bool)
            or context.get("schema_version") != 1
            or context.get("run_id") != run_id
            or isinstance(context.get("loop_index"), bool)
            or context.get("loop_index") != loop_index
            or context.get("scope") != "full_release"
            or not _canonical_json_sha256(context).startswith(filename_match.group(1))
        ):
            raise StateConflictError(
                "durable release invocation context identity is malformed"
            )
        self._require_candidate_binding(context, candidate)

        checks = self._validated_release_context_document(
            context,
            "checks",
            loop_dir / "release-checks.json",
            loop_dir,
        )
        manifest = self._validated_release_context_document(
            context,
            "manifest",
            loop_dir / "release-adapter-manifest.json",
            loop_dir,
        )
        ordinary_evidence = self._validated_release_context_document(
            context,
            "ordinary_evidence",
            loop_dir / "evidence.json",
            loop_dir,
        )
        self._require_candidate_binding(checks, candidate)
        self._require_candidate_binding(manifest, candidate)
        self._require_candidate_binding(ordinary_evidence, candidate)
        check_id = context.get("check_id")
        if (
            not isinstance(check_id, str)
            or not check_id
            or checks.get("scope") != "full_release"
            or checks.get("check_id") != check_id
            or manifest.get("scope") != "full_release"
            or manifest.get("deterministic_check_id") != check_id
        ):
            raise StateConflictError(
                "durable release invocation context check identity is malformed"
            )
        artifacts = context.get("artifacts")
        manifest_artifacts = manifest.get("artifacts")
        if (
            not isinstance(artifacts, Mapping)
            or not isinstance(manifest_artifacts, Mapping)
            or dict(artifacts) != dict(manifest_artifacts)
        ):
            raise StateConflictError(
                "durable release invocation context artifact set is malformed"
            )
        self._validated_manifest_artifact_paths(loop_dir, manifest)
        return context, checks, manifest, retained_path

    def _validated_release_context_document(
        self,
        context: Mapping[str, object],
        field: str,
        expected_path: Path,
        loop_dir: Path,
    ) -> dict[str, object]:
        descriptor = context.get(field)
        if not isinstance(descriptor, Mapping) or set(descriptor) != {
            "path",
            "sha256",
        }:
            raise StateConflictError(
                f"durable release invocation context {field} descriptor is malformed"
            )
        relative = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        ):
            raise StateConflictError(
                f"durable release invocation context {field} descriptor is malformed"
            )
        try:
            retained_path = self._required_regular_evidence_path(
                expected_path, loop_dir
            )
            if relative != self._project_relative(retained_path):
                raise StateConflictError(
                    f"durable release invocation context {field} path is not canonical"
                )
            if _file_sha256(retained_path) != expected_hash:
                raise StateConflictError(
                    f"durable release invocation context {field} hash does not match"
                )
            return _read_json(retained_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            if isinstance(error, StateConflictError):
                raise
            raise StateConflictError(
                f"durable release invocation context {field} artifact is invalid"
            ) from error

    def _validate_release_gate(
        self,
        loop_dir: Path,
        release_gate: Mapping[str, object],
        candidate: Mapping[str, object],
    ) -> None:
        """Validate every retained full-release input before trusting a resumed gate."""

        if not release_gate:
            return
        self._require_candidate_binding(release_gate, candidate)
        if release_gate.get("scope") != "full_release":
            raise StateConflictError("durable release gate has the wrong scope")
        invocation_id = _required_text(release_gate, "invocation_id")
        response_descriptor = _required_mapping(release_gate, "response")
        context_descriptor = _required_mapping(release_gate, "context")
        self._validate_qa_invocation(
            _required_text(candidate, "run_id"),
            _required_positive_int(candidate, "loop_index"),
            loop_dir,
            invocation_id,
            response_descriptor,
            invocation_kind="full-release",
            context_descriptor=context_descriptor,
        )
        check_id = _required_text(release_gate, "check_id")
        if release_gate.get("deterministic_checks_candidate_sha") != candidate.get(
            "candidate_sha"
        ):
            raise StateConflictError("durable release checks target a different candidate")

        context, checks, manifest, _ = self._validated_release_invocation_context(
            _required_text(candidate, "run_id"),
            _required_positive_int(candidate, "loop_index"),
            loop_dir,
            context_descriptor,
            candidate,
        )
        if (
            _required_mapping(release_gate, "checks")
            != _required_mapping(context, "checks")
            or _required_mapping(release_gate, "manifest")
            != _required_mapping(context, "manifest")
        ):
            raise StateConflictError(
                "durable release gate has a different invocation context"
            )
        response = self._read_descriptor(release_gate, "response")
        evidence = self._read_descriptor(release_gate, "evidence")
        for artifact in (checks, manifest, evidence):
            self._require_candidate_binding(artifact, candidate)
        self._revalidate_qa_response(
            _required_positive_int(candidate, "loop_index"),
            loop_dir,
            response,
            evidence,
            candidate,
            manifest,
        )
        if checks.get("scope") != "full_release" or manifest.get("scope") != "full_release":
            raise StateConflictError("durable release records have the wrong scope")
        if checks.get("check_id") != check_id:
            raise StateConflictError("durable release checks have a different identity")
        if manifest.get("deterministic_check_id") != check_id:
            raise StateConflictError("durable release manifest has a different check identity")

        bundle = _required_mapping(checks, "bundle")
        deterministic_passed = (
            bundle.get("status") == "pass" and manifest.get("status") == "pass"
        )
        qa_passed = (
            evidence.get("qa_status") == "pass"
            and evidence.get("product_complete") is True
        )
        if release_gate.get("deterministic_checks_passed") is not deterministic_passed:
            raise StateConflictError("durable release check verdict conflicts with its record")
        if release_gate.get("end_to_end_passed") is not deterministic_passed:
            raise StateConflictError("durable release E2E verdict conflicts with its record")
        expected_qa_status = "pass" if qa_passed else "fail"
        if release_gate.get("qa_status") != expected_qa_status:
            raise StateConflictError("durable release QA verdict conflicts with its evidence")

    def _revalidate_qa_response(
        self,
        loop_index: int,
        loop_dir: Path,
        response: Mapping[str, object],
        evidence: Mapping[str, object],
        candidate: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> None:
        """Re-normalize a journal-bound QA response before applying side effects."""

        try:
            self._validate_schema(Role.QA, response)
            self._validate_role_semantics(Role.QA, response, loop_index)
        except RoleOutputError as error:
            raise StateConflictError(
                "durable QA response no longer satisfies its role contract"
            ) from error
        normalizer = EvidenceNormalizer(
            loop_dir, self.config.project / ".hoh" / "requirements.json"
        )
        replayed = normalizer.normalize(
            dict(response),
            _required_text(candidate, "candidate_sha"),
            _required_text(candidate, "artifact_tree_sha256"),
            manifest,
        )
        if replayed != dict(evidence):
            raise StateConflictError(
                "durable QA response does not match its normalized evidence"
            )

    def _ensure_closure(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        run_state: dict[str, object],
        previous: list[dict[str, object]],
        plan: dict[str, object],
        development: dict[str, object],
        candidate: dict[str, object],
        checks: dict[str, object],
        manifest: dict[str, object],
        qa: dict[str, object],
        started: float,
        elapsed_base: int,
    ) -> tuple[dict[str, object], StopDecision]:
        payload = self._phase_payload(run_id, loop_index, Phase.CLOSURE)
        if payload is not None:
            loop_record = self._read_descriptor(payload, "loop_record")
            self._read_descriptor(payload, "receipt")
            self._validate_completed_closure(
                payload,
                loop_record,
                previous,
                development,
                candidate,
                checks,
                qa,
            )
            decision_data = _required_mapping(payload, "decision")
            should_stop = decision_data.get("should_stop")
            if not isinstance(should_stop, bool):
                raise StateConflictError("durable closure decision is malformed")
            terminal_status = _optional_text(decision_data.get("terminal_status"))
            if should_stop is not (terminal_status is not None):
                raise StateConflictError("durable closure decision is inconsistent")
            return loop_record, StopDecision(
                should_stop,
                terminal_status,
                _required_text(decision_data, "reason"),
            )
        candidate_sha = _required_text(candidate, "candidate_sha")
        evidence = _required_mapping(qa, "evidence")
        ledger = self.issue_ledger.load()
        issues = _mapping_records(ledger.get("issues"))
        acceptance_claim_ids = _acceptance_claim_ids(plan)
        loop_record: dict[str, object] = {
            "loop_index": loop_index,
            "candidate_sha": candidate_sha,
            "normalized_evidence": evidence,
            "deterministic_checks_passed": checks.get("status") == "pass",
            "deterministic_checks_candidate_sha": candidate_sha,
            "deterministic_check_id": _required_text(checks, "check_id"),
            "qa_invocation_id": _required_text(qa, "qa_invocation_id"),
            "issues": issues,
            "issues_candidate_sha": candidate_sha,
            "issue_summary": ledger.get("summary", {}),
            "release_gate": _optional_mapping(qa.get("release_gate")),
            "acceptance_claim_ids": acceptance_claim_ids,
            "changed_paths": list(_string_sequence(development.get("changed_paths"))),
        }
        attempts = self._attempt_receipts(run_id, loop_index, loop_dir)
        receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "loop_index": loop_index,
            "candidate_sha": candidate_sha,
            "attempts": attempts,
        }
        receipt_path = loop_dir / "receipt.json"
        loop_record_path = loop_dir / "loop-record.json"
        atomic_write_json(receipt_path, receipt)
        atomic_write_json(loop_record_path, loop_record)

        all_receipts = [
            *_mapping_records(run_state.get("receipts")),
            *attempts,
        ]
        elapsed_seconds = self._elapsed_seconds(run_state, started, elapsed_base)
        total_tokens = _total_tokens(all_receipts)
        decision = self.policy.evaluate(
            [*previous, loop_record],
            elapsed_seconds=elapsed_seconds,
            total_tokens=total_tokens,
        )
        loop_limit = _required_positive_int(run_state, "loop_limit")
        if not decision.should_stop and loop_index >= loop_limit:
            decision = StopDecision(True, "budget_exhausted", "loop budget exhausted")
        if decision.terminal_status == "complete":
            run_state["best_candidate"] = candidate_sha
            atomic_write_json(
                self.config.project / ".hoh" / "best-candidate.json",
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "loop_index": loop_index,
                    "candidate_sha": candidate_sha,
                },
            )

        selected = self._evidence_commit_paths(
            run_id,
            loop_index,
            loop_dir,
            manifest,
            qa,
            include_best=decision.terminal_status == "complete",
        )
        selected_paths = [self._project_relative(path) for path in selected]
        selected_hashes = {
            self._project_relative(path): _file_sha256(path) for path in selected
        }
        intent_path = loop_dir / "evidence-commit-intent.json"
        prepared = self._load_commit_intent(
            intent_path,
            expected_kind="evidence",
            expected_loop_index=loop_index,
            expected_parent_sha=candidate_sha,
            expected_selected_paths=selected_paths,
            expected_selected_hashes=selected_hashes,
        )
        if prepared is None:
            if self.git.head_sha() != candidate_sha:
                raise StateConflictError(
                    "Git HEAD moved before evidence commit intent was durable"
                )
            prepared = self.git.prepare_evidence(loop_index, tuple(selected))
            if prepared.parent_sha != candidate_sha:
                raise StateConflictError(
                    "prepared evidence has a different expected parent"
                )
            self._write_commit_intent(
                intent_path,
                prepared,
                selected_paths,
                selected_hashes,
            )
        try:
            evidence_commit = self.git.land_prepared_commit(prepared)
        except GitError as error:
            raise StateConflictError(
                "Git HEAD does not match the durable prepared evidence intent"
            ) from error
        loop_record["evidence_commit_sha"] = evidence_commit
        atomic_write_json(loop_record_path, loop_record)
        closure_payload = {
            "loop_record": self._descriptor(loop_record_path),
            "receipt": self._descriptor(receipt_path),
            "evidence_commit_sha": evidence_commit,
            "decision": asdict(decision),
        }
        self.store.complete_phase(
            run_id, loop_index, Phase.CLOSURE, closure_payload
        )
        return loop_record, decision

    def _validate_completed_closure(
        self,
        payload: Mapping[str, object],
        loop_record: Mapping[str, object],
        previous: Sequence[Mapping[str, object]],
        development: Mapping[str, object],
        candidate: Mapping[str, object],
        checks: Mapping[str, object],
        qa: Mapping[str, object],
        *,
        require_head: bool = True,
    ) -> None:
        """Reconcile a journaled closure with Git and all preceding phase records."""

        candidate_sha = _required_text(candidate, "candidate_sha")
        evidence_commit = _required_text(payload, "evidence_commit_sha")
        if loop_record.get("candidate_sha") != candidate_sha:
            raise StateConflictError("durable closure targets a different candidate")
        if loop_record.get("evidence_commit_sha") != evidence_commit:
            raise StateConflictError("durable closure has a different evidence commit")
        try:
            resolved = self.git.rev_parse_in(
                self.config.project, f"{evidence_commit}^{{commit}}"
            )
        except GitError as error:
            raise StateConflictError(
                "durable closure evidence commit cannot be resolved"
            ) from error
        if (
            resolved != evidence_commit
            or not self._is_direct_child(evidence_commit, candidate_sha)
            or (require_head and self.git.head_sha() != evidence_commit)
        ):
            raise StateConflictError(
                "Git HEAD does not match the durable closure evidence commit"
            )

        expected_evidence = _required_mapping(qa, "evidence")
        if loop_record.get("normalized_evidence") != expected_evidence:
            raise StateConflictError("durable closure evidence conflicts with the QA phase")
        if loop_record.get("deterministic_check_id") != checks.get("check_id"):
            raise StateConflictError("durable closure has a different deterministic check")
        if loop_record.get("deterministic_checks_candidate_sha") != candidate_sha:
            raise StateConflictError("durable closure checks target a different candidate")
        if loop_record.get("qa_invocation_id") != qa.get("qa_invocation_id"):
            raise StateConflictError("durable closure has a different QA invocation")
        if loop_record.get("release_gate") != _optional_mapping(qa.get("release_gate")):
            raise StateConflictError("durable closure has a different release gate")
        if loop_record.get("changed_paths") != list(
            _string_sequence(development.get("changed_paths"))
        ):
            raise StateConflictError("durable closure has different production paths")

        decision = _required_mapping(payload, "decision")
        if decision.get("terminal_status") == "complete":
            reevaluated = self.policy.evaluate([*previous, dict(loop_record)])
            if reevaluated.terminal_status != "complete":
                raise StateConflictError(
                    "durable closure completion no longer satisfies stop policy"
                )

    def _closure_decision(self, payload: Mapping[str, object]) -> StopDecision:
        decision = _required_mapping(payload, "decision")
        should_stop = decision.get("should_stop")
        if not isinstance(should_stop, bool):
            raise StateConflictError("durable closure decision is malformed")
        terminal_status = _optional_text(decision.get("terminal_status"))
        if should_stop is not (terminal_status is not None):
            raise StateConflictError("durable closure decision is inconsistent")
        if terminal_status not in {None, "complete", "blocked", "budget_exhausted"}:
            raise StateConflictError("durable closure terminal status is invalid")
        return StopDecision(
            should_stop,
            terminal_status,
            _required_text(decision, "reason"),
        )

    def _validate_candidate_intent(
        self,
        loop_index: int,
        loop_dir: Path,
        development: Mapping[str, object],
        candidate: Mapping[str, object] | None,
    ) -> PreparedCommit:
        base_sha = _required_text(development, "base_sha")
        changed_paths = list(_string_sequence(development.get("changed_paths")))
        manifest = _required_mapping(development, "mutation_manifest")
        entries = self._validate_product_mutation_manifest(
            manifest,
            expected_base_sha=base_sha,
            expected_paths=changed_paths,
        )
        entry_hashes = {
            str(entry["path"]): hashlib.sha256(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for entry in entries
        }
        prepared = self._load_commit_intent(
            loop_dir / "candidate-commit-intent.json",
            expected_kind="candidate",
            expected_loop_index=loop_index,
            expected_parent_sha=base_sha,
            expected_selected_paths=changed_paths,
            expected_selected_hashes=entry_hashes,
        )
        if prepared is None:
            raise StateConflictError("durable candidate phase has no commit intent")
        candidate_sha = (
            _required_text(candidate, "candidate_sha")
            if candidate is not None
            else prepared.commit_sha
        )
        self._validate_prepared_commit(prepared, candidate_sha)
        try:
            changed_in_commit = self.git.changed_paths_between(base_sha, candidate_sha)
        except GitError as error:
            raise StateConflictError("durable candidate diff cannot be resolved") from error
        if changed_in_commit != tuple(changed_paths):
            raise StateConflictError("durable candidate commit has different product paths")
        for entry in entries:
            path = str(entry["path"])
            if entry.get("type") == "deleted":
                try:
                    self.git.file_sha256_at(candidate_sha, path)
                except GitError:
                    continue
                raise StateConflictError("durable candidate kept a deleted product path")
            try:
                digest = self.git.file_sha256_at(candidate_sha, path)
            except GitError as error:
                raise StateConflictError("durable candidate product path is missing") from error
            if digest != entry.get("sha256"):
                raise StateConflictError(
                    "durable candidate product content conflicts with its manifest"
                )
        return prepared

    def _validate_pending_evidence_intent(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        candidate: Mapping[str, object],
        manifest: Mapping[str, object],
        qa: Mapping[str, object],
    ) -> str:
        intent_path = loop_dir / "evidence-commit-intent.json"
        try:
            intent = _read_json(intent_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            raise StateConflictError("durable evidence commit intent is malformed") from error
        prepared_sha = _required_text(intent, "prepared_sha")
        raw_paths = intent.get("selected_paths")
        if not isinstance(raw_paths, list) or any(
            not isinstance(path, str) for path in raw_paths
        ):
            raise StateConflictError("durable evidence intent path set is malformed")
        include_best = ".hoh/best-candidate.json" in raw_paths
        self._validate_evidence_intent(
            run_id,
            loop_index,
            loop_dir,
            candidate,
            manifest,
            qa,
            {"evidence_commit_sha": prepared_sha},
            include_best=include_best,
        )
        return prepared_sha

    def _validate_evidence_intent(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        candidate: Mapping[str, object],
        manifest: Mapping[str, object],
        qa: Mapping[str, object],
        closure_payload: Mapping[str, object],
        *,
        include_best: bool,
    ) -> dict[str, str]:
        intent_path = loop_dir / "evidence-commit-intent.json"
        try:
            intent = _read_json(intent_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            raise StateConflictError("durable evidence commit intent is malformed") from error
        selected_hashes_raw = intent.get("selected_sha256")
        if not isinstance(selected_hashes_raw, Mapping) or not all(
            isinstance(path, str)
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            for path, digest in selected_hashes_raw.items()
        ):
            raise StateConflictError("durable evidence intent hashes are malformed")
        selected_hashes = {
            str(path): str(digest)
            for path, digest in sorted(selected_hashes_raw.items())
        }
        selected = self._evidence_commit_paths(
            run_id,
            loop_index,
            loop_dir,
            manifest,
            qa,
            include_best=include_best,
        )
        selected_paths = [self._project_relative(path) for path in selected]
        if set(selected_hashes) != set(selected_paths):
            raise StateConflictError("durable evidence intent path set is malformed")
        candidate_sha = _required_text(candidate, "candidate_sha")
        prepared = self._load_commit_intent(
            intent_path,
            expected_kind="evidence",
            expected_loop_index=loop_index,
            expected_parent_sha=candidate_sha,
            expected_selected_paths=selected_paths,
            expected_selected_hashes=selected_hashes,
        )
        if prepared is None:  # pragma: no cover - the file was read above
            raise StateConflictError("durable closure has no evidence commit intent")
        evidence_commit = _required_text(closure_payload, "evidence_commit_sha")
        self._validate_prepared_commit(prepared, evidence_commit)
        try:
            changed_in_commit = set(
                self.git.changed_paths_between(candidate_sha, evidence_commit)
            )
        except GitError as error:
            raise StateConflictError("durable evidence commit diff cannot be resolved") from error
        if not changed_in_commit.issubset(set(selected_paths)):
            raise StateConflictError("durable evidence commit contains unselected paths")
        empty_hash = hashlib.sha256(b"").hexdigest()
        for relative in selected_paths:
            expected_hash = selected_hashes[relative]
            try:
                committed_hash = self.git.worktree_file_sha256_at(
                    evidence_commit, relative
                )
            except GitError as error:
                current = self.config.project.joinpath(*PurePosixPath(relative).parts)
                if (
                    expected_hash == empty_hash
                    and current.is_file()
                    and current.stat().st_size == 0
                ):
                    continue
                raise StateConflictError(
                    "durable evidence commit is missing selected content"
                ) from error
            if committed_hash != expected_hash:
                raise StateConflictError(
                    "durable evidence commit content conflicts with its intent"
                )

            if relative.startswith(
                self._project_relative(loop_dir) + "/"
            ) and not relative.endswith("/loop-record.json"):
                current = self.config.project.joinpath(*PurePosixPath(relative).parts)
                if _file_sha256(current) != expected_hash:
                    raise StateConflictError(
                        "durable loop artifact no longer matches its evidence commit"
                    )
        return selected_hashes

    def _validate_prepared_commit(
        self, prepared: PreparedCommit, expected_commit_sha: str
    ) -> None:
        try:
            resolved = self.git.rev_parse_in(
                self.config.project, f"{prepared.commit_sha}^{{commit}}"
            )
            parent = self.git.rev_parse_in(
                self.config.project, f"{prepared.commit_sha}^"
            )
            tree = self.git.rev_parse_in(
                self.config.project, f"{prepared.commit_sha}^{{tree}}"
            )
        except GitError as error:
            raise StateConflictError("durable prepared commit cannot be resolved") from error
        if (
            prepared.commit_sha != expected_commit_sha
            or resolved != prepared.commit_sha
            or parent != prepared.parent_sha
            or tree != prepared.tree_sha
        ):
            raise StateConflictError("durable prepared commit identity conflicts with Git")

    def _invoke_role(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        role: Role,
        prompt: str,
        workspace: Path,
        skills: tuple[SkillDocument, ...],
        *,
        invocation_kind: str,
        protected_paths: tuple[str, ...] | None = None,
        read_only_workspace: Path | None = None,
        response_validator: Callable[[Mapping[str, object]], None] | None = None,
        invocation_context_descriptor: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        requires_invocation_context = (
            role is Role.QA and invocation_kind == "full-release"
        )
        if requires_invocation_context != (
            invocation_context_descriptor is not None
        ):
            raise AssertionError(
                "only full-release QA requires an invocation context descriptor"
            )
        maximum_attempts = 1 + min(self.config.max_role_retries, 1)
        durable_attempts = self._validated_role_attempts(
            run_id, loop_index, loop_dir, role, invocation_kind
        )
        attempts_used = len(durable_attempts)
        if role is Role.QA:
            successful = [
                receipt
                for receipt in durable_attempts
                if receipt.get("outcome") == "success"
            ]
            if successful:
                receipt = successful[0]
                if (
                    invocation_kind == "full-release"
                    and _required_mapping(receipt, "context")
                    != dict(invocation_context_descriptor or {})
                ):
                    raise StateConflictError(
                        "durable full-release QA receipt has a different context"
                    )
                response = self._validated_successful_qa_response(
                    loop_index,
                    loop_dir,
                    receipt,
                    invocation_kind,
                )
                try:
                    if response_validator is not None:
                        response_validator(response)
                except RoleOutputError as error:
                    raise StateConflictError(
                        "durable successful QA response no longer satisfies its role contract"
                    ) from error
                return response, receipt
        if attempts_used >= maximum_attempts:
            raise StateConflictError(
                f"durable {role.value} {invocation_kind} receipt history exhausts "
                "the total attempt allowance"
            )
        current_prompt = (
            self._resume_retry_prompt(prompt, durable_attempts[-1])
            if durable_attempts
            else prompt
        )
        while attempts_used < maximum_attempts:
            attempt = attempts_used + 1
            protected_snapshot = (
                self.git.snapshot_paths(protected_paths)
                if protected_paths is not None
                else None
            )
            read_only_snapshot = (
                (read_only_workspace, _workspace_digest(read_only_workspace))
                if read_only_workspace is not None
                else None
            )
            try:
                return self._invoke_once(
                    run_id,
                    loop_index,
                    loop_dir,
                    role,
                    current_prompt,
                    workspace,
                    skills,
                    invocation_kind=invocation_kind,
                    attempt=attempt,
                    protected_snapshot=protected_snapshot,
                    read_only_snapshot=read_only_snapshot,
                    response_validator=response_validator,
                    invocation_context_descriptor=invocation_context_descriptor,
                )
            except _REPAIRABLE_ROLE_ERRORS as error:
                attempts_used = attempt
                if attempts_used >= maximum_attempts:
                    setattr(error, "hoh_repair_exhausted", True)
                    raise
                current_prompt = self._repair_prompt(prompt, error)
        raise AssertionError("role retry loop did not return or raise")

    def _invoke_once(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        role: Role,
        prompt: str,
        workspace: Path,
        skills: tuple[SkillDocument, ...],
        *,
        invocation_kind: str,
        attempt: int,
        protected_snapshot: Mapping[str, str] | None = None,
        read_only_snapshot: tuple[Path, str] | None = None,
        response_validator: Callable[[Mapping[str, object]], None] | None = None,
        invocation_context_descriptor: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        invocation_id = (
            f"{run_id}:loop-{loop_index:04d}:{role.value}:{invocation_kind}:"
            f"attempt-{attempt:02d}"
        )
        event_name = (
            f"{role.value}-events-attempt-{attempt:02d}.jsonl"
            if invocation_kind == "ordinary"
            else f"{role.value}-{invocation_kind}-events-attempt-{attempt:02d}.jsonl"
        )
        staged_events = (
            self._host_staging_root
            / run_id
            / f"loop-{loop_index:04d}"
            / event_name
        )
        staged_events.parent.mkdir(parents=True, exist_ok=True)
        staged_events.touch(exist_ok=True)
        schema_path = self.schema_dir / _ROLE_SCHEMA[role]
        request = AgentRequest(
            role=role,
            prompt=prompt,
            workspace=workspace,
            sandbox=_ROLE_SANDBOX[role],
            schema_path=schema_path,
            model=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
            timeout_seconds=self.config.role_timeout_minutes * 60,
            events_path=staged_events,
        )
        receipt_path = loop_dir / "receipts" / (
            f"{role.value}-attempt-{attempt:02d}.json"
            if invocation_kind == "ordinary"
            else f"{role.value}-{invocation_kind}-attempt-{attempt:02d}.json"
        )
        result: AgentResult | None = None
        backend_error: BaseException | None = None
        try:
            result = self.backend.run(request)
        except BaseException as error:
            backend_error = error

        boundary_error = self._invocation_boundary_error(
            protected_snapshot, read_only_snapshot
        )
        if boundary_error is not None:
            self._retain_violation_audit(
                run_id,
                loop_index,
                role,
                invocation_kind,
                attempt,
                invocation_id,
                prompt,
                request,
                skills,
                result,
                staged_events,
                event_name,
                receipt_path.name,
                boundary_error,
            )
            raise boundary_error

        if backend_error is not None:
            retained_events = loop_dir / event_name
            self._import_event_stream(staged_events, retained_events)
            receipt = self._receipt(
                invocation_id,
                role,
                invocation_kind,
                attempt,
                prompt,
                request,
                skills,
                None,
                retained_events,
                outcome="error",
                error=backend_error,
            )
            atomic_write_json(receipt_path, receipt)
            raise backend_error

        if result is None:  # pragma: no cover - guards the result/error invariant
            raise AssertionError("backend returned neither a result nor an error")
        retained_events = loop_dir / event_name
        self._import_event_stream(staged_events, retained_events)
        try:
            self._validate_schema(role, result.response)
            if response_validator is not None:
                response_validator(result.response)
        except RoleOutputError as error:
            receipt = self._receipt(
                invocation_id,
                role,
                invocation_kind,
                attempt,
                prompt,
                request,
                skills,
                result,
                retained_events,
                outcome="schema_invalid",
                error=error,
            )
            atomic_write_json(receipt_path, receipt)
            raise
        response_descriptor: dict[str, str] | None = None
        if role is Role.QA:
            response_descriptor = self._persist_successful_qa_response(
                loop_dir, invocation_kind, result.response
            )
        receipt = self._receipt(
            invocation_id,
            role,
            invocation_kind,
            attempt,
            prompt,
            request,
            skills,
            result,
            retained_events,
            outcome="success",
            error=None,
            response_descriptor=response_descriptor,
            context_descriptor=invocation_context_descriptor,
        )
        atomic_write_json(receipt_path, receipt)
        return dict(result.response), receipt

    @staticmethod
    def _qa_response_path(loop_dir: Path, invocation_kind: str) -> Path:
        if invocation_kind == "ordinary":
            return loop_dir / "qa-response.json"
        if invocation_kind == "full-release":
            return loop_dir / "release-qa-response.json"
        raise StateConflictError("durable QA receipt invocation kind is invalid")

    def _persist_successful_qa_response(
        self,
        loop_dir: Path,
        invocation_kind: str,
        response: Mapping[str, object],
    ) -> dict[str, str]:
        """Persist an immutable raw QA response before its success receipt."""

        response_path = self._qa_response_path(loop_dir, invocation_kind)
        response_root = response_path.parent
        if response_root.is_symlink() or (
            response_root.exists() and not response_root.is_dir()
        ):
            raise StateConflictError(
                "durable successful QA response authority is not a directory"
            )
        if response_path.exists() or response_path.is_symlink():
            try:
                retained_path = self._required_regular_evidence_path(
                    response_path, loop_dir
                )
                retained = _read_json(retained_path)
            except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
                raise StateConflictError(
                    "durable successful QA response artifact is malformed"
                ) from error
            if retained != dict(response):
                raise StateConflictError(
                    "durable successful QA response artifact is immutable"
                )
        else:
            atomic_write_json(response_path, dict(response))
        self._required_regular_evidence_path(response_path, loop_dir)
        return self._descriptor(response_path)

    def _validated_successful_qa_response(
        self,
        loop_index: int,
        loop_dir: Path,
        receipt: Mapping[str, object],
        invocation_kind: str,
    ) -> dict[str, object]:
        descriptor = receipt.get("response")
        expected_path = self._qa_response_path(loop_dir, invocation_kind)
        expected_relative = self._project_relative(expected_path)
        if (
            not isinstance(descriptor, Mapping)
            or set(descriptor) != {"path", "sha256"}
            or descriptor.get("path") != expected_relative
            or not isinstance(descriptor.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(descriptor.get("sha256"))) is None
        ):
            raise StateConflictError(
                "durable successful QA receipt response descriptor is malformed"
            )
        try:
            response_path = self._required_regular_evidence_path(
                expected_path, loop_dir
            )
            if _file_sha256(response_path) != descriptor["sha256"]:
                raise StateConflictError(
                    "durable successful QA receipt response hash does not match"
                )
            response = _read_json(response_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            raise StateConflictError(
                "durable successful QA receipt response artifact is invalid"
            ) from error
        try:
            self._validate_schema(Role.QA, response)
            self._validate_role_semantics(Role.QA, response, loop_index)
        except RoleOutputError as error:
            raise StateConflictError(
                "durable successful QA receipt response fails its role contract"
            ) from error
        return response

    def _invocation_boundary_error(
        self,
        protected_snapshot: Mapping[str, str] | None,
        read_only_snapshot: tuple[Path, str] | None,
    ) -> BaseException | None:
        try:
            if protected_snapshot is not None:
                self.git.assert_snapshot_unchanged(protected_snapshot)
            self._assert_read_only_snapshot(read_only_snapshot)
        except BaseException as error:
            return error
        return None

    def _retain_violation_audit(
        self,
        run_id: str,
        loop_index: int,
        role: Role,
        invocation_kind: str,
        attempt: int,
        invocation_id: str,
        prompt: str,
        request: AgentRequest,
        skills: tuple[SkillDocument, ...],
        result: AgentResult | None,
        staged_events: Path,
        event_name: str,
        receipt_name: str,
        error: BaseException,
    ) -> None:
        """Retain boundary-failure audit outside the now-untrusted product tree."""

        audit_dir = (
            self._host_staging_root
            / run_id
            / f"loop-{loop_index:04d}"
            / "audit"
        )
        retained_events = audit_dir / event_name
        data = staged_events.read_bytes() if staged_events.exists() else b""
        self._atomic_write_bytes(retained_events, data)
        staged_events.unlink(missing_ok=True)
        event_location = (
            "host-staging/"
            + retained_events.relative_to(self._host_staging_root).as_posix()
        )
        receipt = self._receipt(
            invocation_id,
            role,
            invocation_kind,
            attempt,
            prompt,
            request,
            skills,
            result,
            event_location,
            outcome="policy_violation",
            error=error,
        )
        atomic_write_json(audit_dir / "receipts" / receipt_name, receipt)

    @staticmethod
    def _assert_read_only_snapshot(
        snapshot: tuple[Path, str] | None,
    ) -> None:
        if snapshot is None:
            return
        workspace, expected = snapshot
        if _workspace_digest(workspace) != expected:
            raise RoleOutputError("read-only role modified its workspace")

    @staticmethod
    def _repair_prompt(prompt: str, error: BaseException) -> str:
        category = (
            "structured output validation"
            if isinstance(error, RoleOutputError)
            else "transient backend execution"
        )
        return (
            "# REPAIR RETRY\n\n"
            f"The previous fresh invocation failed {category}. "
            "This is the single allowed repair attempt. Re-evaluate the supplied public "
            "context and return only output conforming to the required schema.\n\n"
            + prompt
        )

    @staticmethod
    def _resume_retry_prompt(
        prompt: str, prior_receipt: Mapping[str, object]
    ) -> str:
        outcome = prior_receipt.get("outcome")
        if outcome not in {"success", "schema_invalid", "error"}:
            raise StateConflictError(
                "durable receipt outcome does not permit a resumed invocation"
            )
        if outcome == "error":
            error = prior_receipt.get("error")
            if not isinstance(error, Mapping) or error.get("type") not in {
                BackendTimeout.__name__,
                BackendProcessError.__name__,
            }:
                raise StateConflictError(
                    "durable receipt records a non-repairable role failure"
                )
        return (
            "# CRASH-SAFE FRESH RETRY\n\n"
            "A prior invocation has a durable receipt, but this phase did not "
            "complete. This is the final allowed fresh attempt. Re-evaluate the "
            "supplied public context and return only output conforming to the "
            "required schema.\n\n"
            + prompt
        )

    def _receipt(
        self,
        invocation_id: str,
        role: Role,
        invocation_kind: str,
        attempt: int,
        prompt: str,
        request: AgentRequest,
        skills: tuple[SkillDocument, ...],
        result: AgentResult | None,
        events_path: Path | str,
        *,
        outcome: str,
        error: BaseException | None,
        response_descriptor: Mapping[str, object] | None = None,
        context_descriptor: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        usage = asdict(result.usage) if result is not None else asdict(_empty_usage())
        receipt: dict[str, object] = {
            "invocation_id": invocation_id,
            "role": role.value,
            "kind": invocation_kind,
            "attempt": attempt,
            "outcome": outcome,
            "model": request.model,
            "reasoning_effort": request.reasoning_effort,
            "sandbox": request.sandbox.value,
            "timeout_seconds": request.timeout_seconds,
            "schema_path": request.schema_path.name,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "events_path": (
                events_path
                if isinstance(events_path, str)
                else self._project_relative(events_path)
            ),
            "skills": [
                {
                    "skill_id": skill.skill_id,
                    "version": skill.version,
                    "sha256": skill.sha256,
                }
                for skill in skills
            ],
            "usage": usage,
            "return_code": result.return_code if result is not None else None,
            "executable_version": (
                result.executable_version if result is not None else "unknown"
            ),
        }
        if error is not None:
            receipt["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        if role is Role.QA and outcome == "success":
            if response_descriptor is None:
                raise AssertionError(
                    "a successful QA receipt requires a durable response descriptor"
                )
            receipt["response"] = dict(response_descriptor)
            if invocation_kind == "full-release":
                if context_descriptor is None:
                    raise AssertionError(
                        "a successful full-release QA receipt requires a durable context"
                    )
                receipt["context"] = dict(context_descriptor)
            elif context_descriptor is not None:
                raise AssertionError(
                    "ordinary QA receipts must not bind a release context"
                )
        elif response_descriptor is not None:
            raise AssertionError(
                "only a successful QA receipt may bind a response descriptor"
            )
        elif context_descriptor is not None:
            raise AssertionError(
                "only a successful full-release QA receipt may bind a context"
            )
        return receipt

    def _prompt_context(
        self,
        *,
        workspace: Path,
        previous: Sequence[Mapping[str, object]],
        current_plan: Mapping[str, object],
        checks: object,
    ) -> dict[str, object]:
        prd = (self.config.project / ".hoh" / "prd.md").read_text(encoding="utf-8")
        requirements = _read_json(
            self.config.project / ".hoh" / "requirements.json"
        )
        public_prd = (
            prd.rstrip()
            + "\n\n## Requirement registry\n\n"
            + json.dumps(requirements, ensure_ascii=False, indent=2, sort_keys=True)
        )
        return {
            "PUBLIC_PRD": public_prd,
            "PROJECT_SUMMARY": self.adapter.summarize(workspace),
            "PREVIOUS_EVIDENCE": [
                dict(_required_mapping(loop, "normalized_evidence")) for loop in previous
            ],
            "ISSUE_LEDGER": self.issue_ledger.load(),
            "CURRENT_PLAN": dict(current_plan),
            "CHECKS": checks,
        }

    def _developer_skills(
        self, plan: Mapping[str, object]
    ) -> tuple[SkillDocument, ...]:
        if self.config.adapter != "godot":
            return self.skills.select(Role.DEVELOPER, self.config.adapter, ())
        rendered = json.dumps(plan, ensure_ascii=False).lower()
        requested = ["godot.runtime-testing"]
        if "asset" in rendered:
            requested.append("godot.asset-pipeline")
        if any(term in rendered for term in ("ui", "ux", "layout", "accessib")):
            requested.append("godot.ui-ux-polish")
        if any(term in rendered for term in ("performance", "profil", "frame time")):
            requested.append("godot.performance-tuning")
        return self.skills.select(Role.DEVELOPER, self.config.adapter, tuple(requested))

    def _qa_skills(self) -> tuple[SkillDocument, ...]:
        requested = ["core.evidence-grounded-qa"]
        if self.config.adapter == "godot":
            requested.append("godot.runtime-testing")
        return self.skills.select(Role.QA, self.config.adapter, tuple(requested))

    def _with_frozen_candidate(
        self,
        run_id: str,
        loop_index: int,
        candidate_sha: str,
        operation: Callable[[Path], object],
    ) -> object:
        if self.git.head_sha() != candidate_sha:
            raise ResumeError("current Git HEAD does not match the durable candidate")
        worktree = self._qa_worktree_factory(
            self.git,
            self.config.project
            / ".hoh"
            / "tmp"
            / f"qa-{run_id}-loop-{loop_index:04d}-{uuid.uuid4().hex[:8]}",
        )
        frozen = worktree.create(candidate_sha)
        body_error: BaseException | None = None
        try:
            if self.git.rev_parse_in(frozen, "HEAD") != candidate_sha:
                raise ResumeError("QA worktree is not bound to the durable candidate")
            if self.git.current_branch_in(frozen):
                raise ResumeError("QA worktree must use detached HEAD")
            before = _workspace_digest(frozen)
            result = operation(frozen)
            try:
                head_after = self.git.rev_parse_in(frozen, "HEAD")
                branch_after = self.git.current_branch_in(frozen)
            except GitError as error:
                raise RoleOutputError(
                    "read-only candidate operation damaged its Git identity"
                ) from error
            if head_after != candidate_sha or branch_after:
                raise RoleOutputError(
                    "read-only candidate operation moved the frozen candidate HEAD"
                )
            if _workspace_digest(frozen) != before:
                raise RoleOutputError("read-only candidate operation modified the worktree")
            return result
        except BaseException as error:
            body_error = error
            raise
        finally:
            try:
                worktree.remove()
            except GitError as cleanup_error:
                if body_error is not None:
                    raise QaWorktreeCleanupError(body_error, cleanup_error) from cleanup_error
                raise

    def _adapter_manifest_artifacts(
        self,
        loop_dir: Path,
        output: Path,
        bundle: CheckBundle,
        collected: Mapping[str, str],
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        output_prefix = output.relative_to(loop_dir).as_posix()
        for relative, supplied_hash in collected.items():
            if not isinstance(relative, str) or not isinstance(supplied_hash, str):
                raise EvidenceBindingError("adapter manifest hashes must be strings")
            candidate = (output / relative).resolve()
            if not candidate.is_relative_to(output.resolve()) or not candidate.is_file():
                raise EvidenceBindingError("adapter artifact is outside the host output")
            actual = _file_sha256(candidate)
            if actual != supplied_hash:
                raise EvidenceBindingError("adapter artifact hash does not match its file")
            artifacts[f"{output_prefix}/{relative}"] = actual
        for relative in bundle.artifact_paths:
            candidate = (loop_dir / relative).resolve()
            if candidate.is_relative_to(loop_dir.resolve()) and candidate.is_file():
                artifacts[Path(relative).as_posix()] = _file_sha256(candidate)
        return dict(sorted(artifacts.items()))

    def _phase_payload(
        self, run_id: str, loop_index: int, phase: Phase
    ) -> dict[str, object] | None:
        state = self.store.phase_state(run_id, loop_index)
        completed = state.get("completed")
        if not isinstance(completed, Mapping):
            raise StateError("phase journal completed field is invalid")
        record = completed.get(phase.value)
        if record is None:
            return None
        if not isinstance(record, Mapping):
            raise StateError(f"completed phase {phase.value} is invalid")
        expected = f"{run_id}:{loop_index}:{phase.value}"
        if record.get("idempotency_key") != expected:
            raise StateConflictError(f"completed phase {phase.value} has a wrong key")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise StateError(f"completed phase {phase.value} payload is invalid")
        return payload

    def _read_descriptor(
        self, payload: Mapping[str, object], field: str
    ) -> dict[str, object]:
        descriptor = payload.get(field)
        if not isinstance(descriptor, Mapping):
            raise StateError(f"phase payload is missing {field}")
        relative = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise StateError(f"phase payload descriptor {field} is invalid")
        path = (self.config.project / relative).resolve()
        if not path.is_relative_to(self.config.project.resolve()):
            raise StateError(f"phase payload descriptor {field} escapes the project")
        if not path.is_file() or _file_sha256(path) != expected_hash:
            raise StateConflictError(f"durable phase artifact {field} failed validation")
        return _read_json(path)

    def _descriptor(self, path: Path) -> dict[str, str]:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.config.project.resolve()):
            raise StateError("durable phase artifacts must be inside the project")
        return {
            "path": resolved.relative_to(self.config.project.resolve()).as_posix(),
            "sha256": _file_sha256(resolved),
        }

    def _validate_schema(self, role: Role, response: Mapping[str, object]) -> None:
        schema_path = self.schema_dir / _ROLE_SCHEMA[role]
        try:
            schema = _read_json(schema_path)
            Draft202012Validator(schema).validate(dict(response))
        except (OSError, json.JSONDecodeError) as error:
            raise OrchestratorError(f"could not load role schema: {schema_path.name}") from error
        except ValidationError as error:
            location = ".".join(str(item) for item in error.absolute_path) or "response"
            raise RoleSchemaError(
                f"{role.value} output failed schema at {location}: {error.message}"
            ) from error

    def _validate_role_semantics(
        self, role: Role, response: Mapping[str, object], loop_index: int
    ) -> None:
        if role in {Role.PLANNER, Role.QA} and response.get("iteration") != loop_index:
            raise RoleSchemaError(
                f"{role.value} output iteration does not match loop {loop_index}"
            )
        if role is Role.PLANNER:
            priorities = response.get("priorities")
            if (
                not isinstance(priorities, list)
                or len(priorities) > self.config.max_priorities_per_loop
            ):
                raise RoleSchemaError("planner output exceeds the configured priority limit")

    def _validate_candidate(self, candidate: Mapping[str, object]) -> None:
        candidate_sha = _required_text(candidate, "candidate_sha")
        if self.git.rev_parse_in(
            self.config.project, f"{candidate_sha}^{{commit}}"
        ) != candidate_sha:
            raise StateConflictError("durable candidate commit cannot be resolved")
        tree_id = self.git.rev_parse_in(
            self.config.project, f"{candidate_sha}^{{tree}}"
        )
        expected_tree = hashlib.sha256(tree_id.encode("ascii")).hexdigest()
        if candidate.get("artifact_tree_sha256") != expected_tree:
            raise StateConflictError("durable candidate tree identity does not match Git")

    def _is_direct_child(self, child_sha: str, parent_sha: str) -> bool:
        try:
            return (
                self.git.rev_parse_in(self.config.project, f"{child_sha}^")
                == parent_sha
            )
        except GitError:
            return False

    def _write_commit_intent(
        self,
        path: Path,
        prepared: PreparedCommit,
        selected_paths: Sequence[str],
        selected_hashes: Mapping[str, str],
    ) -> None:
        atomic_write_json(
            path,
            {
                "schema_version": 1,
                "kind": prepared.kind,
                "loop_index": prepared.loop_index,
                "parent_sha": prepared.parent_sha,
                "prepared_sha": prepared.commit_sha,
                "tree_sha": prepared.tree_sha,
                "selected_paths": list(selected_paths),
                "selected_sha256": dict(sorted(selected_hashes.items())),
            },
        )

    def _load_commit_intent(
        self,
        path: Path,
        *,
        expected_kind: str,
        expected_loop_index: int,
        expected_parent_sha: str,
        expected_selected_paths: Sequence[str],
        expected_selected_hashes: Mapping[str, str],
    ) -> PreparedCommit | None:
        if not path.exists():
            return None
        try:
            document = _read_json(path)
            allowed = {
                "schema_version",
                "kind",
                "loop_index",
                "parent_sha",
                "prepared_sha",
                "tree_sha",
                "selected_paths",
                "selected_sha256",
            }
            if set(document) != allowed or document.get("schema_version") != 1:
                raise StateConflictError("durable commit intent schema is malformed")
            selected_paths = list(_string_sequence(document.get("selected_paths")))
            selected_hashes_raw = document.get("selected_sha256")
            if not isinstance(selected_hashes_raw, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in selected_hashes_raw.items()
            ):
                raise StateConflictError(
                    "durable commit intent selected hashes are malformed"
                )
            selected_hashes = dict(sorted(selected_hashes_raw.items()))
            if (
                document.get("kind") != expected_kind
                or document.get("loop_index") != expected_loop_index
                or document.get("parent_sha") != expected_parent_sha
                or selected_paths != list(expected_selected_paths)
                or selected_hashes != dict(sorted(expected_selected_hashes.items()))
            ):
                raise StateConflictError(
                    f"durable {expected_kind} commit intent conflicts with host inputs"
                )
            prepared = PreparedCommit(
                kind=_required_text(document, "kind"),
                loop_index=_required_positive_int(document, "loop_index"),
                parent_sha=_required_text(document, "parent_sha"),
                commit_sha=_required_text(document, "prepared_sha"),
                tree_sha=_required_text(document, "tree_sha"),
            )
        except (json.JSONDecodeError, OSError, StateError, ValueError, TypeError) as error:
            if isinstance(error, StateConflictError):
                raise
            raise StateConflictError("durable commit intent is malformed") from error
        return prepared

    @staticmethod
    def _require_candidate_binding(
        record: Mapping[str, object], candidate: Mapping[str, object]
    ) -> None:
        if record.get("candidate_sha") != candidate.get("candidate_sha"):
            raise StateConflictError("durable artifact targets a different candidate")
        if record.get("artifact_tree_sha256") != candidate.get("artifact_tree_sha256"):
            raise StateConflictError("durable artifact targets a different candidate tree")

    def _production_changed_paths(self, base_sha: str) -> tuple[str, ...]:
        protected = tuple(
            Path(path).as_posix().rstrip("/") for path in self.config.protected_paths
        )
        return tuple(
            path
            for path in self.git.changed_paths(base_sha)
            if not any(path == root or path.startswith(root + "/") for root in protected)
        )

    def _product_mutation_manifest(
        self, base_sha: str, paths: Sequence[str]
    ) -> dict[str, object]:
        try:
            document = self.git.candidate_mutation_manifest(
                base_sha, tuple(paths)
            )
        except GitError as error:
            raise StateConflictError(
                "product mutations could not be captured in a durable Git manifest"
            ) from error
        self._validate_product_mutation_manifest(
            document,
            expected_base_sha=base_sha,
            expected_paths=paths,
        )
        return document

    def _assert_product_mutation_matches(
        self,
        base_sha: str,
        changed_paths: Sequence[str],
        manifest: Mapping[str, object],
    ) -> dict[str, str]:
        entries = self._validate_product_mutation_manifest(
            manifest,
            expected_base_sha=base_sha,
            expected_paths=changed_paths,
        )
        current_paths = self._production_changed_paths(base_sha)
        if tuple(changed_paths) != current_paths:
            raise StateConflictError(
                "current product mutation paths do not match the durable manifest"
            )
        try:
            current_manifest = self.git.candidate_mutation_manifest(
                base_sha, current_paths
            )
        except GitError as error:
            raise StateConflictError(
                "current product mutations could not be validated against Git"
            ) from error
        if current_manifest != dict(manifest):
            raise StateConflictError(
                "current product mutation content does not match the durable manifest"
            )
        return {
            str(entry["path"]): hashlib.sha256(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for entry in entries
        }

    def _validate_product_mutation_manifest(
        self,
        manifest: Mapping[str, object],
        *,
        expected_base_sha: str,
        expected_paths: Sequence[str],
    ) -> list[dict[str, object]]:
        if (
            set(manifest) != {"schema_version", "base_sha", "entries"}
            or manifest.get("schema_version") != 1
            or manifest.get("base_sha") != expected_base_sha
        ):
            raise StateConflictError("durable product mutation manifest is malformed")
        raw_entries = manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise StateConflictError("durable product mutation manifest entries are malformed")
        entries: list[dict[str, object]] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
                "path",
                "type",
                "sha256",
                "mode",
            }:
                raise StateConflictError(
                    "durable product mutation manifest entry is malformed"
                )
            path = raw_entry.get("path")
            entry_type = raw_entry.get("type")
            digest = raw_entry.get("sha256")
            mode = raw_entry.get("mode")
            if not isinstance(path, str):
                raise StateConflictError(
                    "durable product mutation manifest path is malformed"
                )
            self._product_path(path)
            if entry_type == "deleted":
                if digest is not None or mode is not None:
                    raise StateConflictError(
                        "durable deleted product mutation has an index entry"
                    )
            elif entry_type == "file":
                if not isinstance(digest, str) or re.fullmatch(
                    r"[0-9a-f]{64}", digest
                ) is None:
                    raise StateConflictError(
                        "durable product mutation content hash is malformed"
                    )
                if not isinstance(mode, str) or mode not in {"100644", "100755"}:
                    raise StateConflictError(
                        "durable product mutation file mode is malformed"
                    )
            elif entry_type == "symlink":
                if not isinstance(digest, str) or re.fullmatch(
                    r"[0-9a-f]{64}", digest
                ) is None:
                    raise StateConflictError(
                        "durable product mutation content hash is malformed"
                    )
                if mode != "120000":
                    raise StateConflictError(
                        "durable product mutation symlink mode is malformed"
                    )
            else:
                raise StateConflictError(
                    "durable product mutation type is malformed"
                )
            entries.append(dict(raw_entry))
        entry_paths = [str(entry["path"]) for entry in entries]
        if (
            entry_paths != sorted(entry_paths)
            or len(entry_paths) != len(set(entry_paths))
            or entry_paths != list(expected_paths)
        ):
            raise StateConflictError(
                "durable product mutation manifest path set is ambiguous"
            )
        return entries

    def _product_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise StateConflictError("product mutation path is not normalized")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or relative != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0].endswith(":")
        ):
            raise StateConflictError("product mutation path is not normalized")
        protected = tuple(
            PurePosixPath(item).as_posix().rstrip("/")
            for item in self.config.protected_paths
        )
        if any(
            relative == root or relative.startswith(root + "/")
            for root in protected
        ):
            raise StateConflictError("product mutation path is protected")
        return self.config.project.joinpath(*path.parts)

    def _attempt_receipts(
        self, run_id: str, loop_index: int, loop_dir: Path
    ) -> list[dict[str, object]]:
        receipts = [
            receipt
            for _, receipt, external in self._validated_receipt_records(
                run_id, loop_index, loop_dir
            )
            if not external
        ]
        return sorted(receipts, key=_receipt_sort_key)

    def _validated_role_attempts(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        role: Role,
        kind: str,
    ) -> list[dict[str, object]]:
        return [
            receipt
            for _, receipt, _ in self._validated_receipt_records(
                run_id, loop_index, loop_dir
            )
            if receipt.get("role") == role.value and receipt.get("kind") == kind
        ]

    def _validated_receipt_records(
        self, run_id: str, loop_index: int, loop_dir: Path
    ) -> list[tuple[Path, dict[str, object], bool]]:
        maximum_attempts = 1 + min(self.config.max_role_retries, 1)
        external_dir = (
            self._host_staging_root
            / run_id
            / f"loop-{loop_index:04d}"
            / "audit"
            / "receipts"
        )
        records: list[tuple[Path, dict[str, object], bool]] = []
        indexed: dict[tuple[str, str, int], tuple[Path, bool]] = {}
        pattern = re.compile(
            r"^(planner|developer|qa)(?:-(full-release))?-attempt-(\d{2})\.json$"
        )
        for directory, external in (
            (loop_dir / "receipts", False),
            (external_dir, True),
        ):
            if directory.is_symlink():
                raise StateConflictError(
                    "durable receipt directory must not be a symlink"
                )
            if not directory.exists():
                continue
            if not directory.is_dir():
                raise StateConflictError(
                    "durable receipt directory is not a regular directory"
                )
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if path.is_symlink() or not path.is_file():
                    raise StateConflictError(
                        "durable receipt directory contains a non-regular entry"
                    )
                match = pattern.fullmatch(path.name)
                if match is None:
                    raise StateConflictError("durable receipt filename is malformed")
                role_value, release_marker, attempt_text = match.groups()
                kind = "full-release" if release_marker else "ordinary"
                if kind == "full-release" and role_value != Role.QA.value:
                    raise StateConflictError("durable receipt invocation kind is invalid")
                attempt = int(attempt_text)
                if attempt < 1 or attempt > maximum_attempts:
                    raise StateConflictError(
                        "durable receipt exceeds the total attempt allowance"
                    )
                key = (role_value, kind, attempt)
                if key in indexed:
                    raise StateConflictError("durable receipt attempt is duplicated")
                indexed[key] = (path, external)

        grouped: dict[tuple[str, str], list[int]] = {}
        for role_value, kind, attempt in indexed:
            grouped.setdefault((role_value, kind), []).append(attempt)
        for attempts in grouped.values():
            ordered = sorted(attempts)
            if ordered != list(range(1, len(ordered) + 1)):
                raise StateConflictError("durable receipt attempt sequence is ambiguous")

        successful_qa_groups: set[tuple[str, str]] = set()
        for (role_value, kind, attempt), (path, external) in sorted(
            indexed.items(), key=lambda item: item[0]
        ):
            try:
                receipt = _read_json(path)
            except (OSError, json.JSONDecodeError, StateError) as error:
                raise StateConflictError("durable receipt history is malformed") from error
            invocation_id = (
                f"{run_id}:loop-{loop_index:04d}:{role_value}:{kind}:"
                f"attempt-{attempt:02d}"
            )
            outcome = receipt.get("outcome")
            metadata_attempt = receipt.get("attempt")
            executable_version = receipt.get("executable_version")
            if (
                receipt.get("invocation_id") != invocation_id
                or receipt.get("role") != role_value
                or receipt.get("kind") != kind
                or isinstance(metadata_attempt, bool)
                or not isinstance(metadata_attempt, int)
                or metadata_attempt != attempt
                or outcome
                not in {"success", "schema_invalid", "error", "policy_violation"}
                or not isinstance(executable_version, str)
                or not executable_version.strip()
            ):
                raise StateConflictError("durable receipt attempt metadata is malformed")
            if external is not (outcome == "policy_violation"):
                raise StateConflictError(
                    "durable boundary-audit receipt is stored in the wrong authority"
                )
            qa_group = (role_value, kind)
            if role_value == Role.QA.value and qa_group in successful_qa_groups:
                raise StateConflictError(
                    "durable QA receipt history continues after a successful identity"
                )
            if outcome == "success":
                if "error" in receipt:
                    raise StateConflictError("durable success receipt is ambiguous")
                if role_value == Role.QA.value:
                    self._validated_successful_qa_response(
                        loop_index, loop_dir, receipt, kind
                    )
                    if kind == "full-release":
                        context_descriptor = receipt.get("context")
                        if not isinstance(context_descriptor, Mapping):
                            raise StateConflictError(
                                "durable successful full-release QA receipt has no context"
                            )
                        self._validated_release_invocation_context(
                            run_id,
                            loop_index,
                            loop_dir,
                            context_descriptor,
                        )
                    elif "context" in receipt:
                        raise StateConflictError(
                            "durable ordinary QA receipt has an ambiguous context"
                        )
                    successful_qa_groups.add(qa_group)
                elif "response" in receipt or "context" in receipt:
                    raise StateConflictError(
                        "durable non-QA receipt has ambiguous QA artifacts"
                    )
            else:
                if role_value == Role.QA.value and (
                    "response" in receipt or "context" in receipt
                ):
                    raise StateConflictError(
                        "durable failed QA receipt has ambiguous QA artifacts"
                    )
                error = receipt.get("error")
                if (
                    not isinstance(error, Mapping)
                    or not isinstance(error.get("type"), str)
                    or not str(error.get("type")).strip()
                    or not isinstance(error.get("message"), str)
                ):
                    raise StateConflictError("durable failure receipt is malformed")
            event_name = (
                f"{role_value}-events-attempt-{attempt:02d}.jsonl"
                if kind == "ordinary"
                else f"{role_value}-{kind}-events-attempt-{attempt:02d}.jsonl"
            )
            expected_event = (
                "host-staging/"
                + (
                    Path(run_id)
                    / f"loop-{loop_index:04d}"
                    / "audit"
                    / event_name
                ).as_posix()
                if external
                else self._project_relative(loop_dir / event_name)
            )
            if receipt.get("events_path") != expected_event:
                raise StateConflictError("durable receipt event identity is malformed")
            records.append((path, receipt, external))
        return records

    def _evidence_commit_paths(
        self,
        run_id: str,
        loop_index: int,
        loop_dir: Path,
        manifest: Mapping[str, object],
        qa: Mapping[str, object],
        *,
        include_best: bool,
    ) -> list[Path]:
        """Return only explicitly named, host-known artifacts for this loop."""

        selected: list[Path] = []
        for name in (
            "plan.json",
            "developer-report.json",
            "product-mutation.json",
            "candidate-commit-intent.json",
            "candidate.json",
            "baseline-checks.json",
            "checks.json",
            "adapter-manifest.json",
            "qa-response.json",
            "evidence.json",
            "receipt.json",
            "loop-record.json",
        ):
            selected.append(
                self._required_regular_evidence_path(loop_dir / name, loop_dir)
            )
        selected.extend(self._validated_manifest_artifact_paths(loop_dir, manifest))

        release_gate = _optional_mapping(qa.get("release_gate"))
        if release_gate:
            context_descriptor = _required_mapping(release_gate, "context")
            _, _, release_manifest, context_path = (
                self._validated_release_invocation_context(
                    run_id,
                    loop_index,
                    loop_dir,
                    context_descriptor,
                )
            )
            for name in (
                "release-gate.json",
                "release-checks.json",
                "release-adapter-manifest.json",
                "release-qa-response.json",
                "release-evidence.json",
            ):
                selected.append(
                    self._required_regular_evidence_path(loop_dir / name, loop_dir)
                )
            selected.append(context_path)
            selected.extend(
                self._validated_manifest_artifact_paths(loop_dir, release_manifest)
            )

        for receipt_path, receipt, external in self._validated_receipt_records(
            run_id, loop_index, loop_dir
        ):
            if external:
                raise StateConflictError(
                    "a boundary-audit failure cannot be included in loop closure"
                )
            selected.append(
                self._required_regular_evidence_path(receipt_path, loop_dir)
            )
            event_relative = receipt.get("events_path")
            if not isinstance(event_relative, str):
                raise StateConflictError("durable receipt event path is malformed")
            event_path = self.config.project.joinpath(
                *PurePosixPath(event_relative).parts
            )
            selected.append(
                self._required_regular_evidence_path(event_path, loop_dir)
            )

        selected.append(
            self._required_regular_evidence_path(
                self.config.project / ".hoh" / "issue-ledger.json",
                self.config.project / ".hoh",
            )
        )
        if include_best:
            selected.append(
                self._required_regular_evidence_path(
                    self.config.project / ".hoh" / "best-candidate.json",
                    self.config.project / ".hoh",
                )
            )
        return sorted(set(selected), key=lambda path: path.as_posix())

    def _validated_manifest_artifact_paths(
        self, loop_dir: Path, manifest: Mapping[str, object]
    ) -> list[Path]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise StateConflictError("durable adapter manifest artifacts are malformed")
        if not all(
            isinstance(relative, str) and isinstance(digest, str)
            for relative, digest in artifacts.items()
        ):
            raise StateConflictError(
                "durable adapter manifest artifact identity is malformed"
            )
        selected: list[Path] = []
        for relative, expected_hash in sorted(artifacts.items()):
            if (
                re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            ):
                raise StateConflictError(
                    "durable adapter manifest artifact identity is malformed"
                )
            path = PurePosixPath(relative)
            if (
                not relative
                or "\\" in relative
                or path.is_absolute()
                or relative != path.as_posix()
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[0].endswith(":")
            ):
                raise StateConflictError(
                    "durable adapter manifest artifact path is not normalized"
                )
            artifact_path = self._required_regular_evidence_path(
                loop_dir.joinpath(*path.parts), loop_dir
            )
            if _file_sha256(artifact_path) != expected_hash:
                raise StateConflictError(
                    "durable adapter manifest artifact hash does not match"
                )
            selected.append(artifact_path)
        return selected

    def _required_regular_evidence_path(self, path: Path, root: Path) -> Path:
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise StateConflictError(
                "selected evidence path is outside its authority"
            ) from error
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise StateConflictError(
                    "selected evidence path must not traverse a symlink"
                )
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise StateConflictError(
                "selected evidence path is missing, non-regular, or outside its authority"
            )
        return resolved

    def _record_closed_loop(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        loop_record: dict[str, object],
        decision: StopDecision,
        elapsed_seconds: int,
    ) -> None:
        loop_index = _required_positive_int(loop_record, "loop_index")
        loops = _mapping_records(run_state.get("loops"))
        existing = next(
            (item for item in loops if item.get("loop_index") == loop_index), None
        )
        if existing is None:
            loops.append(loop_record)
        elif existing != loop_record:
            raise StateConflictError("closed loop record conflicts with durable run state")
        run_state["loops"] = loops
        run_state["receipts"] = [
            receipt
            for index in range(1, loop_index + 1)
            for receipt in self._attempt_receipts(
                _required_text(run_state, "run_id"),
                index,
                run_dir / "loops" / f"loop-{index:04d}",
            )
        ]
        run_state["skill_receipts"] = _unique_skill_receipts(
            _mapping_records(run_state.get("receipts"))
        )
        run_state["current_candidate"] = loop_record.get("candidate_sha")
        if decision.terminal_status == "complete":
            run_state["best_candidate"] = loop_record.get("candidate_sha")
        run_state["elapsed_seconds"] = elapsed_seconds
        run_state["status"] = (
            decision.terminal_status if decision.should_stop else "running"
        )
        run_state["reason"] = decision.reason
        run_state["updated_at"] = self._now().isoformat()
        self._write_run_state(run_dir, run_state)

    def _terminal_result(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        decision: StopDecision,
    ) -> dict[str, object]:
        status = build_status(
            run_state,
            receipts=_mapping_records(run_state.get("receipts")),
            decision=decision,
            issue_ledger=self.issue_ledger,
        )
        write_run_summary(run_dir, render_run_summary(status))
        result = dict(status)
        result["loops_completed"] = status["completed_loops"]
        result["status"] = status["terminal_status"]
        return result

    def _persist_failure(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        error: BaseException,
    ) -> None:
        category, repairable = self._classify_failure(error)
        failure = {
            "category": category,
            "code": type(error).__name__,
            "message": str(error),
            "repairable": repairable,
        }
        run_state["failure"] = failure
        run_state["status"] = "resumable" if repairable else "blocked"
        run_state["reason"] = (
            f"resumable {category} failure: {type(error).__name__}"
            if repairable
            else f"unrecoverable {category} failure: {type(error).__name__}"
        )
        run_state["updated_at"] = self._now().isoformat()
        external = self._host_staging_root / _required_text(run_state, "run_id") / "failure.json"
        atomic_write_json(external, failure)
        try:
            self._write_run_state(run_dir, run_state)
            if not repairable:
                decision = StopDecision(True, "blocked", str(run_state["reason"]))
                status = build_status(
                    run_state,
                    receipts=self._attempt_receipts_for_run(run_dir),
                    decision=decision,
                    issue_ledger=self.issue_ledger,
                )
                write_run_summary(run_dir, render_run_summary(status))
        except BaseException:
            # The external record remains authoritative when protected state was damaged.
            pass

    def _persist_cancellation(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        elapsed_seconds: int,
    ) -> dict[str, object]:
        """Record user cancellation as a terminal, non-resumable outcome."""

        run_id = _required_text(run_state, "run_id")
        cancellation: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "cancelled",
            "reason": "run cancelled by user",
        }
        atomic_write_json(self._host_staging_root / run_id / "cancellation.json", cancellation)
        receipts = self._attempt_receipts_for_run(run_dir)
        run_state.pop("failure", None)
        run_state["cancellation"] = cancellation
        run_state["receipts"] = receipts
        run_state["skill_receipts"] = _unique_skill_receipts(receipts)
        run_state["elapsed_seconds"] = elapsed_seconds
        run_state["status"] = "cancelled"
        run_state["reason"] = "run cancelled by user"
        run_state["updated_at"] = self._now().isoformat()
        self._write_run_state(run_dir, run_state)
        return self._terminal_result(
            run_dir,
            run_state,
            StopDecision(True, "cancelled", "run cancelled by user"),
        )

    @staticmethod
    def _classify_failure(error: BaseException) -> tuple[str, bool]:
        if isinstance(error, PreflightError):
            return "infrastructure", False
        if isinstance(error, _PROTOCOL_ERRORS):
            return "protocol", False
        if isinstance(error, _RETRYABLE_BACKEND_ERRORS):
            return "infrastructure", not bool(
                getattr(error, "hoh_repair_exhausted", False)
            )
        if isinstance(error, (GitError, StateError, OSError)):
            return "infrastructure", True
        return "infrastructure", True

    def _latest_durable_run(self) -> tuple[Path, dict[str, object]]:
        runs_root = self.config.project / ".hoh" / "runs"
        candidates: list[tuple[str, str, Path, dict[str, object]]] = []
        if runs_root.is_dir():
            for metadata_path in sorted(runs_root.glob("*/run.json")):
                try:
                    state = _read_json(metadata_path)
                except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
                    raise StateConflictError(
                        f"could not read durable run state: {metadata_path}"
                    ) from error
                run_id = state.get("run_id")
                if run_id != metadata_path.parent.name:
                    raise StateConflictError(
                        "durable run metadata does not match its directory"
                    )
                updated = state.get("updated_at")
                candidates.append(
                    (
                        updated if isinstance(updated, str) else "",
                        metadata_path.parent.name,
                        metadata_path.parent,
                        state,
                    )
                )
        if not candidates:
            raise ResumeError("no durable HoH run exists")
        _, _, run_dir, state = max(candidates, key=lambda item: (item[0], item[1]))
        return run_dir, state

    def _durable_loop_directories(self, run_dir: Path) -> list[tuple[int, Path]]:
        loops_root = run_dir / "loops"
        if not loops_root.exists():
            return []
        if loops_root.is_symlink() or not loops_root.is_dir():
            raise StateConflictError("durable loops path is not a regular directory")
        result: list[tuple[int, Path]] = []
        pattern = re.compile(r"loop-(\d{4})")
        for path in sorted(loops_root.iterdir(), key=lambda item: item.name):
            match = pattern.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_dir():
                raise StateConflictError("durable loops directory contains an invalid entry")
            result.append((int(match.group(1)), path))
        indices = [index for index, _ in result]
        if indices != list(range(1, len(indices) + 1)):
            raise StateConflictError("durable loop directory sequence is not contiguous")
        return result

    def _inspection_decision(
        self,
        run_state: Mapping[str, object],
        terminal_decision: StopDecision | None,
        run_id: str,
    ) -> StopDecision:
        raw_status = run_state.get("status")
        if not isinstance(raw_status, str) or raw_status not in _DURABLE_RUN_STATUSES:
            raise StateConflictError("durable run state has an invalid status")
        if terminal_decision is not None:
            if raw_status == terminal_decision.terminal_status:
                if run_state.get("reason") != terminal_decision.reason:
                    raise StateConflictError(
                        "durable run reason conflicts with its terminal closure"
                    )
            elif raw_status in {"resumable", "blocked"}:
                self._validated_failure(run_state, run_id)
            elif raw_status != "running":
                raise StateConflictError(
                    "durable run status conflicts with its terminal closure"
                )
            return terminal_decision
        if raw_status == "cancelled":
            return self._validated_cancellation(run_state, run_id)
        if raw_status in {"resumable", "blocked"}:
            return self._validated_failure(run_state, run_id)
        if raw_status != "running":
            raise StateConflictError("durable terminal status has no closure record")
        return StopDecision(
            False,
            "running",
            _optional_text(run_state.get("reason")) or "run is in progress",
        )

    def _validated_failure(
        self, run_state: Mapping[str, object], run_id: str
    ) -> StopDecision:
        failure = run_state.get("failure")
        if not isinstance(failure, Mapping):
            raise StateConflictError("durable failed run has no structured failure")
        external_path = self._host_staging_root / run_id / "failure.json"
        try:
            external = _read_json(external_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            raise StateConflictError("durable external failure record is unavailable") from error
        if dict(failure) != external:
            raise StateConflictError("durable failure records conflict")
        category = failure.get("category")
        code = failure.get("code")
        repairable = failure.get("repairable")
        if (
            category not in {"infrastructure", "protocol"}
            or not isinstance(code, str)
            or not code
            or not isinstance(repairable, bool)
        ):
            raise StateConflictError("durable failure record is malformed")
        status = "resumable" if repairable else "blocked"
        reason = (
            f"resumable {category} failure: {code}"
            if repairable
            else f"unrecoverable {category} failure: {code}"
        )
        if run_state.get("status") != status or run_state.get("reason") != reason:
            raise StateConflictError("durable failure aggregate is inconsistent")
        return StopDecision(True, status, reason)

    def _validated_cancellation(
        self, run_state: Mapping[str, object], run_id: str
    ) -> StopDecision:
        cancellation = run_state.get("cancellation")
        if not isinstance(cancellation, Mapping):
            raise StateConflictError("durable cancelled run has no cancellation record")
        external_path = self._host_staging_root / run_id / "cancellation.json"
        try:
            external = _read_json(external_path)
        except (OSError, UnicodeError, json.JSONDecodeError, StateError) as error:
            raise StateConflictError(
                "durable external cancellation record is unavailable"
            ) from error
        expected = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "cancelled",
            "reason": "run cancelled by user",
        }
        if dict(cancellation) != expected or external != expected:
            raise StateConflictError("durable cancellation records conflict")
        if (
            run_state.get("status") != "cancelled"
            or run_state.get("reason") != "run cancelled by user"
        ):
            raise StateConflictError("durable cancellation aggregate is inconsistent")
        return StopDecision(True, "cancelled", "run cancelled by user")

    def _validate_run_aggregate(
        self,
        run_state: Mapping[str, object],
        loops: Sequence[Mapping[str, object]],
        receipts: Sequence[Mapping[str, object]],
        candidates: Sequence[str],
        best_candidate: str | None,
        decision: StopDecision,
    ) -> None:
        raw_loops = run_state.get("loops")
        if not isinstance(raw_loops, list) or any(
            not isinstance(item, Mapping) for item in raw_loops
        ):
            raise StateConflictError("durable aggregate loops are malformed")
        aggregate_loops = [dict(item) for item in raw_loops]
        if len(aggregate_loops) > len(loops) or aggregate_loops != [
            dict(item) for item in loops[: len(aggregate_loops)]
        ]:
            raise StateConflictError("durable aggregate loops conflict with phase records")

        aggregate_candidate = run_state.get("current_candidate")
        if aggregate_candidate is not None and aggregate_candidate not in set(candidates):
            raise StateConflictError(
                "durable aggregate current candidate is not phase-bound"
            )
        aggregate_best = run_state.get("best_candidate")
        valid_best_candidates = (
            {best_candidate} if best_candidate is not None else set(candidates)
        )
        if aggregate_best is not None and aggregate_best not in valid_best_candidates:
            raise StateConflictError("durable aggregate best candidate is not closure-bound")

        raw_receipts = run_state.get("receipts")
        if not isinstance(raw_receipts, list) or any(
            not isinstance(item, Mapping) for item in raw_receipts
        ):
            raise StateConflictError("durable aggregate receipts are malformed")
        aggregate_receipts = [dict(item) for item in raw_receipts]
        if len(aggregate_receipts) > len(receipts) or aggregate_receipts != [
            dict(item) for item in receipts[: len(aggregate_receipts)]
        ]:
            raise StateConflictError(
                "durable aggregate receipts conflict with retained receipts"
            )
        elapsed = run_state.get("elapsed_seconds", 0)
        if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
            raise StateConflictError("durable aggregate elapsed time is malformed")
        if decision.terminal_status == "complete" and best_candidate is None:
            raise StateConflictError("durable completion has no candidate")

    def _latest_resumable_run(self) -> tuple[Path, dict[str, object]]:
        runs_root = self.config.project / ".hoh" / "runs"
        candidates: list[tuple[str, Path, dict[str, object]]] = []
        if runs_root.is_dir():
            for metadata_path in runs_root.glob("*/run.json"):
                try:
                    state = _read_json(metadata_path)
                except (OSError, json.JSONDecodeError, StateError):
                    continue
                if state.get("status") not in {"running", "resumable"}:
                    continue
                updated = state.get("updated_at")
                candidates.append(
                    (updated if isinstance(updated, str) else "", metadata_path.parent, state)
                )
        if not candidates:
            raise ResumeError("no resumable HoH run exists")
        _, run_dir, state = max(candidates, key=lambda item: (item[0], item[1].name))
        if state.get("run_id") != run_dir.name:
            raise ResumeError("resumable run metadata does not match its directory")
        return run_dir, state

    def _attempt_receipts_for_run(self, run_dir: Path) -> list[dict[str, object]]:
        return [
            _read_json(path)
            for path in sorted(run_dir.glob("loops/loop-*/receipts/*.json"))
        ]

    def _write_run_state(
        self, run_dir: Path, run_state: Mapping[str, object]
    ) -> None:
        atomic_write_json(run_dir / "run.json", run_state)

    def _import_event_stream(self, staged: Path, destination: Path) -> None:
        data = staged.read_bytes() if staged.exists() else b""
        self._atomic_write_bytes(destination, data)
        staged.unlink(missing_ok=True)

    @staticmethod
    def _atomic_write_bytes(destination: Path, data: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _project_relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.config.project.resolve()).as_posix()

    def _loop_limit(self, supplied: int | None) -> int:
        if supplied is None:
            return self.config.max_loops
        if isinstance(supplied, bool) or not isinstance(supplied, int) or supplied < 1:
            raise ValueError("max_loops must be a positive integer")
        return min(supplied, self.config.max_loops)

    def _elapsed_seconds(
        self,
        run_state: Mapping[str, object],
        started: float,
        elapsed_base: int,
    ) -> int:
        session_elapsed = elapsed_base + max(0, int(self._monotonic() - started))
        return max(session_elapsed, self._elapsed_from_timestamp(run_state))

    def _elapsed_from_timestamp(self, run_state: Mapping[str, object]) -> int:
        started_at = run_state.get("started_at")
        if not isinstance(started_at, str):
            return 0
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return 0
        return max(0, int((self._now() - started).total_seconds()))

    def _validate_composition(self) -> None:
        if self.config.project.resolve() != self.git.repository:
            raise ValueError("config project and Git repository must match")
        missing = [
            name
            for name in _ROLE_SCHEMA.values()
            if not (self.schema_dir / name).is_file()
        ]
        if missing:
            raise ValueError("missing role schemas: " + ", ".join(sorted(missing)))

    @staticmethod
    def _default_run_id() -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:12]}"


def _bundle_document(bundle: CheckBundle) -> dict[str, object]:
    return {
        "adapter": bundle.adapter,
        "status": bundle.status,
        "results": [asdict(result) for result in bundle.results],
        "artifact_paths": list(bundle.artifact_paths),
    }


def _workspace_digest(root: Path) -> str:
    digest = hashlib.sha256()
    root = root.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        encoded = relative.as_posix().encode("utf-8", errors="surrogateescape")
        if path.is_symlink():
            digest.update(b"L\0" + encoded + b"\0" + os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"D\0" + encoded + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + encoded + b"\0" + _file_sha256(path).encode("ascii"))
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(document: Mapping[str, object]) -> str:
    try:
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise StateConflictError(
            "durable release invocation context is not canonical JSON"
        ) from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as source:
        document = json.load(source)
    if not isinstance(document, dict):
        raise StateError(f"state document {path.name} is not an object")
    return document


def _required_mapping(
    record: Mapping[str, object], field: str
) -> dict[str, object]:
    value = record.get(field)
    if not isinstance(value, Mapping):
        raise StateError(f"required mapping field is missing: {field}")
    return dict(value)


def _optional_mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _required_text(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise StateError(f"required text field is missing: {field}")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _required_positive_int(record: Mapping[str, object], field: str) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StateError(f"required positive integer field is missing: {field}")
    return value


def _mapping_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _acceptance_claim_ids(plan: Mapping[str, object]) -> list[str]:
    claims: list[str] = list(_string_sequence(plan.get("acceptance_gate")))
    priorities = plan.get("priorities")
    if isinstance(priorities, list):
        for priority in priorities:
            if isinstance(priority, Mapping):
                claims.extend(_string_sequence(priority.get("acceptance_claims")))
    return sorted(set(claims))


def _total_tokens(receipts: Sequence[Mapping[str, object]]) -> int:
    total = 0
    for receipt in receipts:
        usage = receipt.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for field in ("input_tokens", "output_tokens", "reasoning_output_tokens"):
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                total += value
    return total


def _receipt_sort_key(receipt: Mapping[str, object]) -> tuple[int, int, int, str]:
    role_rank = {"planner": 0, "developer": 1, "qa": 2}
    role = receipt.get("role")
    kind = receipt.get("kind")
    attempt = receipt.get("attempt")
    return (
        role_rank.get(role if isinstance(role, str) else "", 99),
        0 if kind == "ordinary" else 1,
        attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 99,
        str(receipt.get("invocation_id", "")),
    )


def _unique_skill_receipts(
    receipts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for receipt in receipts:
        skills = receipt.get("skills")
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if not isinstance(skill, Mapping):
                continue
            skill_id = skill.get("skill_id")
            sha256 = skill.get("sha256")
            if isinstance(skill_id, str) and isinstance(sha256, str):
                unique[(skill_id, sha256)] = dict(skill)
    return [unique[key] for key in sorted(unique)]


def _empty_usage() -> AgentUsage:
    return AgentUsage()
