"""Host-owned HoH loop orchestration and crash-safe phase resume."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

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
from hoh.vcs.git import GitError, GitService, ProtectedPathError
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
        self.git.assert_clean()
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
        return self._drive(run_dir, run_state)

    def resume(self) -> dict[str, object]:
        """Continue the newest durable resumable run from its first incomplete phase."""

        run_dir, run_state = self._latest_resumable_run()
        branch = run_state.get("branch")
        if (
            not isinstance(branch, str)
            or self.git.current_branch_in(self.config.project) != branch
        ):
            raise ResumeError("the resumable run branch is not checked out")
        return self._drive(run_dir, run_state)

    def _drive(
        self, run_dir: Path, run_state: dict[str, object]
    ) -> dict[str, object]:
        lock = RunLock(self.config.project / ".hoh" / "lock")
        run_id = _required_text(run_state, "run_id")
        lock.acquire()
        started = self._monotonic()
        try:
            while True:
                loop_index = _required_positive_int(run_state, "active_loop")
                loop_record, decision = self._execute_loop(
                    run_dir, run_state, loop_index, started
                )
                self._record_closed_loop(run_dir, run_state, loop_record, decision)
                if decision.should_stop:
                    return self._terminal_result(run_dir, run_state, decision)
                run_state["active_loop"] = loop_index + 1
                run_state["updated_at"] = self._now().isoformat()
                self._write_run_state(run_dir, run_state)
        except BaseException as error:
            self._persist_failure(run_dir, run_state, error)
            raise
        finally:
            lock.release()

    def _execute_loop(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        loop_index: int,
        started: float,
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
            qa,
            started,
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
            return {
                "report": self._read_descriptor(payload, "report"),
                "base_sha": _required_text(payload, "base_sha"),
                "changed_paths": list(_string_sequence(payload.get("changed_paths"))),
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
        report_path = loop_dir / "developer-report.json"
        atomic_write_json(report_path, report)
        phase_payload = {
            "report": self._descriptor(report_path),
            "base_sha": base_sha,
            "changed_paths": list(changed_paths),
        }
        self.store.complete_phase(
            run_id, loop_index, Phase.DEVELOPMENT, phase_payload
        )
        return {"report": report, "base_sha": base_sha, "changed_paths": list(changed_paths)}

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
        head_sha = self.git.head_sha()
        if head_sha == base_sha:
            candidate_sha = self.git.commit_candidate(loop_index, summary)
        elif self._is_direct_child(head_sha, base_sha):
            # The candidate commit may have reached Git immediately before the
            # process died, leaving the atomic phase journal one step behind.
            # Developer invocations protect .git, so a single direct child is
            # the only safe commit-shaped crash window to recover here.
            candidate_sha = head_sha
        else:
            raise StateConflictError(
                "Git HEAD cannot be reconciled with the incomplete candidate phase"
            )
        tree_id = self.git.rev_parse_in(
            self.config.project, f"{candidate_sha}^{{tree}}"
        )
        candidate = {
            "schema_version": 1,
            "run_id": run_id,
            "loop_index": loop_index,
            "candidate_sha": candidate_sha,
            "artifact_tree_sha256": hashlib.sha256(tree_id.encode("ascii")).hexdigest(),
            "changed_paths": list(_string_sequence(development.get("changed_paths"))),
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
            evidence = self._read_descriptor(payload, "evidence")
            self._require_candidate_binding(evidence, candidate)
            self._revalidate_normalized_evidence(
                loop_dir, evidence, candidate, manifest
            )
            self.issue_ledger.apply(evidence, loop_index)
            release_gate = _optional_mapping(payload.get("release_gate"))
            self._validate_release_gate(loop_dir, release_gate, candidate)
            return {
                "evidence": evidence,
                "qa_invocation_id": _required_text(payload, "qa_invocation_id"),
                "release_gate": release_gate,
            }
        candidate_sha = _required_text(candidate, "candidate_sha")
        skills = self._qa_skills()

        def qa(frozen: Path) -> tuple[dict[str, object], str]:
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
            return raw, _required_text(receipt, "invocation_id")

        raw, invocation_id = self._with_frozen_candidate(
            run_id, loop_index, candidate_sha, qa
        )
        normalizer = EvidenceNormalizer(
            loop_dir, self.config.project / ".hoh" / "requirements.json"
        )
        normalized = normalizer.normalize(
            raw,
            candidate_sha,
            _required_text(candidate, "artifact_tree_sha256"),
            manifest,
        )
        evidence_path = loop_dir / "evidence.json"
        atomic_write_json(evidence_path, normalized)
        self.issue_ledger.apply(normalized, loop_index)
        release_gate: dict[str, object] = {}
        if self._ordinary_candidate_is_release_ready(normalized, manifest):
            release_gate = self._run_release_gate(
                run_id,
                loop_index,
                loop_dir,
                previous,
                plan,
                candidate,
                normalized,
                skills,
            )
        phase_payload: dict[str, object] = {
            "evidence": self._descriptor(evidence_path),
            "qa_invocation_id": invocation_id,
            "release_gate": release_gate,
        }
        self.store.complete_phase(run_id, loop_index, Phase.QA, phase_payload)
        return {
            "evidence": normalized,
            "qa_invocation_id": invocation_id,
            "release_gate": release_gate,
        }

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

        def release(
            frozen: Path,
        ) -> tuple[dict[str, object], dict[str, object], dict[str, object], str]:
            bundle = self.adapter.check(AdapterContext(frozen, output), plan)
            collected = self.adapter.collect(AdapterContext(frozen, output), bundle)
            checks: dict[str, object] = {
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
            manifest: dict[str, object] = {
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
            atomic_write_json(checks_path, checks)
            atomic_write_json(manifest_path, manifest)
            context_checks = {
                "scope": "full_release",
                "candidate_sha": candidate_sha,
                "artifact_tree_sha256": artifact_tree_sha256,
                "deterministic_check_id": check_id,
                "ordinary_evidence": ordinary_evidence,
                "checks": checks,
                "adapter_manifest": manifest,
            }
            prompt = (
                "# FULL RELEASE QA / E2E GATE\n\n"
                "This is a distinct fresh full-release invocation against the same frozen "
                "candidate. Re-run independent end-to-end acceptance from the retained "
                "full-release check records; do not reuse the ordinary loop verdict.\n\n"
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
            raw, receipt = self._invoke_role(
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
            )
            return checks, manifest, raw, _required_text(receipt, "invocation_id")

        checks, manifest, raw, invocation_id = self._with_frozen_candidate(
            run_id, loop_index, candidate_sha, release
        )
        normalizer = EvidenceNormalizer(
            loop_dir, self.config.project / ".hoh" / "requirements.json"
        )
        normalized = normalizer.normalize(
            raw,
            candidate_sha,
            artifact_tree_sha256,
            manifest,
        )
        release_evidence_path = loop_dir / "release-evidence.json"
        atomic_write_json(release_evidence_path, normalized)
        checks_path = loop_dir / "release-checks.json"
        manifest_path = loop_dir / "release-adapter-manifest.json"
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
            "checks": self._descriptor(checks_path),
            "manifest": self._descriptor(manifest_path),
            "evidence": self._descriptor(release_evidence_path),
        }

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
        _required_text(release_gate, "invocation_id")
        check_id = _required_text(release_gate, "check_id")
        if release_gate.get("deterministic_checks_candidate_sha") != candidate.get(
            "candidate_sha"
        ):
            raise StateConflictError("durable release checks target a different candidate")

        checks = self._read_descriptor(release_gate, "checks")
        manifest = self._read_descriptor(release_gate, "manifest")
        evidence = self._read_descriptor(release_gate, "evidence")
        for artifact in (checks, manifest, evidence):
            self._require_candidate_binding(artifact, candidate)
        self._revalidate_normalized_evidence(
            loop_dir, evidence, candidate, manifest
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

    def _revalidate_normalized_evidence(
        self,
        loop_dir: Path,
        evidence: Mapping[str, object],
        candidate: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> None:
        """Replay Task 10 normalization to recheck cited files and host derivations."""

        host_metadata = evidence.get("host_metadata")
        if not isinstance(host_metadata, Mapping) or not isinstance(
            host_metadata.get("qa_product_complete"), bool
        ):
            raise StateConflictError(
                "durable normalized evidence is missing host completion metadata"
            )
        reconstructed_raw = dict(evidence)
        reconstructed_raw["product_complete"] = host_metadata["qa_product_complete"]
        reconstructed_raw.pop("host_metadata", None)
        reconstructed_raw.pop("diagnostics", None)
        normalizer = EvidenceNormalizer(
            loop_dir, self.config.project / ".hoh" / "requirements.json"
        )
        replayed = normalizer.normalize(
            reconstructed_raw,
            _required_text(candidate, "candidate_sha"),
            _required_text(candidate, "artifact_tree_sha256"),
            manifest,
        )
        if replayed != dict(evidence):
            raise StateConflictError(
                "durable normalized evidence no longer matches host derivation"
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
        qa: dict[str, object],
        started: float,
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
        attempts = self._attempt_receipts(loop_dir)
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
        elapsed_seconds = self._elapsed_seconds(run_state, started)
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

        selected = self._evidence_commit_paths(loop_dir)
        best_path = self.config.project / ".hoh" / "best-candidate.json"
        if best_path.is_file():
            selected.append(best_path)
        head_sha = self.git.head_sha()
        if head_sha == candidate_sha:
            evidence_commit = self.git.commit_evidence(loop_index, tuple(selected))
        elif self._is_direct_child(head_sha, candidate_sha):
            # As with candidate creation, Git can be durable before the phase
            # journal.  Reuse that exact direct child instead of creating a
            # duplicate evidence commit on resume.
            evidence_commit = head_sha
        else:
            raise StateConflictError(
                "Git HEAD cannot be reconciled with the incomplete closure phase"
            )
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
            or self.git.head_sha() != evidence_commit
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
    ) -> tuple[dict[str, object], dict[str, object]]:
        current_prompt = prompt
        maximum_attempts = 1 + min(self.config.max_role_retries, 1)
        for invocation_number in range(1, maximum_attempts + 1):
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
                    protected_snapshot=protected_snapshot,
                    read_only_snapshot=read_only_snapshot,
                    response_validator=response_validator,
                )
            except _REPAIRABLE_ROLE_ERRORS as error:
                if invocation_number >= maximum_attempts:
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
        protected_snapshot: Mapping[str, str] | None = None,
        read_only_snapshot: tuple[Path, str] | None = None,
        response_validator: Callable[[Mapping[str, object]], None] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        attempt = self._next_attempt(loop_dir, role, invocation_kind)
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
        try:
            result = self.backend.run(request)
        except BaseException as error:
            if protected_snapshot is not None:
                self.git.assert_snapshot_unchanged(protected_snapshot)
            self._assert_read_only_snapshot(read_only_snapshot)
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
                error=error,
            )
            atomic_write_json(receipt_path, receipt)
            raise

        if protected_snapshot is not None:
            self.git.assert_snapshot_unchanged(protected_snapshot)
        self._assert_read_only_snapshot(read_only_snapshot)
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
        )
        atomic_write_json(receipt_path, receipt)
        return dict(result.response), receipt

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
        events_path: Path,
        *,
        outcome: str,
        error: BaseException | None,
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
            "events_path": self._project_relative(events_path),
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

    def _attempt_receipts(self, loop_dir: Path) -> list[dict[str, object]]:
        receipts_dir = loop_dir / "receipts"
        if not receipts_dir.is_dir():
            return []
        receipts = [
            _read_json(path)
            for path in receipts_dir.glob("*.json")
        ]
        return sorted(receipts, key=_receipt_sort_key)

    def _next_attempt(self, loop_dir: Path, role: Role, kind: str) -> int:
        prefix = f"{role.value}-" if kind == "ordinary" else f"{role.value}-{kind}-"
        existing = list((loop_dir / "receipts").glob(f"{prefix}attempt-*.json"))
        return len(existing) + 1

    def _evidence_commit_paths(self, loop_dir: Path) -> list[Path]:
        selected_names = {
            "plan.json",
            "developer-report.json",
            "candidate.json",
            "baseline-checks.json",
            "checks.json",
            "adapter-manifest.json",
            "evidence.json",
            "release-checks.json",
            "release-adapter-manifest.json",
            "release-evidence.json",
            "receipt.json",
            "loop-record.json",
        }
        selected = [
            path
            for path in loop_dir.rglob("*")
            if path.is_file()
            and (
                path.name in selected_names
                or path.suffix == ".jsonl"
                or "receipts" in path.relative_to(loop_dir).parts
                or path.relative_to(loop_dir).parts[0]
                in {"adapter", "release-adapter"}
            )
        ]
        selected.append(self.config.project / ".hoh" / "issue-ledger.json")
        return sorted(set(selected), key=lambda path: path.as_posix())

    def _record_closed_loop(
        self,
        run_dir: Path,
        run_state: dict[str, object],
        loop_record: dict[str, object],
        decision: StopDecision,
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
                run_dir / "loops" / f"loop-{index:04d}"
            )
        ]
        run_state["skill_receipts"] = _unique_skill_receipts(
            _mapping_records(run_state.get("receipts"))
        )
        run_state["current_candidate"] = loop_record.get("candidate_sha")
        if decision.terminal_status == "complete":
            run_state["best_candidate"] = loop_record.get("candidate_sha")
        run_state["elapsed_seconds"] = self._elapsed_from_timestamp(run_state)
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = staged.read_bytes() if staged.exists() else b""
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
            staged.unlink(missing_ok=True)

    def _project_relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.config.project.resolve()).as_posix()

    def _loop_limit(self, supplied: int | None) -> int:
        if supplied is None:
            return self.config.max_loops
        if isinstance(supplied, bool) or not isinstance(supplied, int) or supplied < 1:
            raise ValueError("max_loops must be a positive integer")
        return min(supplied, self.config.max_loops)

    def _elapsed_seconds(self, run_state: Mapping[str, object], started: float) -> int:
        prior = run_state.get("elapsed_seconds", 0)
        prior_seconds = prior if isinstance(prior, int) and not isinstance(prior, bool) else 0
        session_elapsed = prior_seconds + max(0, int(self._monotonic() - started))
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
