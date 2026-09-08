from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hoh.adapters.base import AdapterContext
from hoh.backends import AgentResult, FakeAgentBackend, FakeResponse
from hoh.models import CheckBundle, CheckResult, Diagnostic, HarnessConfig
from hoh.orchestrator import HoHOrchestrator
from hoh.policy import StopPolicy
from hoh.skills.registry import SkillRegistry
from hoh.state.store import StateStore
from hoh.vcs.git import GitService


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def initialized_product(tmp_path: Path) -> Path:
    project = tmp_path / "product"
    project.mkdir()
    run_git(project, "init", "--initial-branch=main")
    (project / "product.txt").write_text("original\n", encoding="utf-8")
    state = project / ".hoh"
    state.mkdir()
    (state / "prd.md").write_text("# Product\n\nShip the observable behavior.\n", encoding="utf-8")
    (state / "requirements.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claims": [
                    {"id": "claim-main", "description": "Main behavior works", "required": True},
                    {"id": "claim-polish", "description": "Polish remains", "required": False},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "issue-ledger.json").write_text(
        '{"schema_version": 1, "issues": []}\n', encoding="utf-8"
    )
    run_git(project, "add", ".")
    run_git(
        project,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "initial",
    )
    return project


class RecordingAdapter:
    def __init__(self, *, crash_on_first_check: bool = False) -> None:
        self.crash_on_first_check = crash_on_first_check
        self.baseline_calls = 0
        self.check_calls = 0

    def doctor(self, project: Path) -> tuple[()]:
        del project
        return ()

    def summarize(self, project: Path) -> dict[str, object]:
        return {"sha": run_git(project, "rev-parse", "HEAD"), "product": "text"}

    def baseline(self, context: AdapterContext, plan: dict[str, object]) -> CheckBundle:
        del plan
        self.baseline_calls += 1
        context.output.mkdir(parents=True, exist_ok=True)
        return CheckBundle(
            "recording",
            "pass",
            (CheckResult("baseline", "pass", "baseline passed"),),
        )

    def check(self, context: AdapterContext, plan: dict[str, object]) -> CheckBundle:
        del plan
        self.check_calls += 1
        if self.crash_on_first_check and self.check_calls == 1:
            raise RuntimeError("simulated crash after candidate")
        artifact = context.output / "artifacts" / "proof.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            (context.project / "product.txt").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return CheckBundle(
            "recording",
            "pass",
            (CheckResult("candidate", "pass", "candidate passed"),),
        )

    def collect(self, context: AdapterContext, bundle: CheckBundle) -> dict[str, str]:
        del bundle
        artifact = context.output / "artifacts" / "proof.txt"
        return {"artifacts/proof.txt": hashlib.sha256(artifact.read_bytes()).hexdigest()}


class BlockedAdapter(RecordingAdapter):
    def doctor(self, project: Path) -> tuple[Diagnostic, ...]:
        del project
        return (
            Diagnostic("tool:missing", "blocked", "Required executable is missing"),
        )


class ScriptedBackend:
    """Small agent-boundary fake that can raise configured backend failures."""

    def __init__(
        self,
        actions: list[FakeResponse | BaseException],
        *,
        executable_version: str = "scripted-agent/2",
    ) -> None:
        self.actions = list(actions)
        self.executable_version = executable_version
        self.requests = []

    def run(self, request) -> AgentResult:
        self.requests.append(request)
        if not self.actions:
            raise AssertionError("no scripted backend action remains")
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if action.on_run is not None:
            action.on_run(request)
        response = action.response(request) if callable(action.response) else action.response
        return AgentResult(
            dict(response),
            action.usage,
            0,
            self.executable_version,
        )


@dataclass
class Services:
    orchestrator: HoHOrchestrator
    backend: object
    adapter: RecordingAdapter
    git: GitService


def plan(iteration: int = 1) -> dict[str, object]:
    return {
        "iteration": iteration,
        "objective": "Change the observable product",
        "priorities": [
            {
                "id": "priority-main",
                "title": "Change product",
                "implementation_target": "product.txt",
                "acceptance_claims": ["claim-main"],
                "source_gap_ids": [],
            }
        ],
        "preservation_constraints": [],
        "acceptance_gate": ["claim-main"],
    }


def developer_change(request, content: str = "changed\n") -> None:
    (request.workspace / "product.txt").write_text(content, encoding="utf-8")


def qa_response(
    request,
    *,
    iteration: int = 1,
    include_major_gap: bool = True,
    candidate_sha: str | None = None,
) -> dict[str, object]:
    candidates = re.findall(r'"candidate_sha":\s*"([0-9a-f]{40,64})"', request.prompt)
    trees = re.findall(r'"artifact_tree_sha256":\s*"([0-9a-f]{64})"', request.prompt)
    artifact_path = next(
        value
        for value in (
            "release-adapter/artifacts/proof.txt",
            "adapter/artifacts/proof.txt",
            "artifacts/proof.txt",
        )
        if value in request.prompt
    )
    assert candidates
    assert trees
    manifest_match = re.search(
        rf'"{re.escape(artifact_path)}":\s*"([0-9a-f]{{64}})"', request.prompt
    )
    assert manifest_match is not None
    return {
        "iteration": iteration,
        "candidate_sha": candidate_sha or candidates[-1],
        "artifact_tree_sha256": trees[-1],
        "qa_status": "pass",
        "product_complete": False,
        "verified_records": [
            {
                "claim_id": "claim-main",
                "claim": "Main behavior works",
                "observations": ["The retained proof records changed output."],
                "execution_records": [
                    {
                        "type": "text-check",
                        "path": artifact_path,
                        "sha256": manifest_match.group(1),
                        "observation": "changed output was observed",
                    }
                ],
                "preservation_requirement": "Keep the behavior working",
            }
        ],
        "gap_records": (
            [
                {
                    "claim_id": "claim-polish",
                    "severity": "major",
                    "impact": "The product still needs polish.",
                    "observations": ["No polish evidence exists."],
                    "recommended_update": "Add polish.",
                    "validation_requirement": "Run the polish check.",
                }
            ]
            if include_major_gap
            else []
        ),
        "planner_handoff": {
            "preservation_constraints": ["Preserve claim-main"],
            "update_targets": ["claim-polish"],
            "validation_requirements": ["Run the polish check"],
        },
    }


def developer_response(summary: str = "change product") -> dict[str, object]:
    return {
        "summary": summary,
        "completed_priority_ids": ["priority-main"],
        "self_tests": ["manual fixture write"],
        "known_gaps": [],
    }


def config_for(project: Path, **overrides: object) -> HarnessConfig:
    values: dict[str, object] = {
        "project": project,
        "adapter": "command",
        "model": "offline-model",
        "reasoning_effort": "low",
        "codex_bin": "codex",
        "max_loops": 12,
        "max_priorities_per_loop": 3,
        "max_role_retries": 1,
        "max_consecutive_no_progress": 3,
        "max_consecutive_same_blocker": 3,
        "role_timeout_minutes": 1,
        "max_total_tokens": 100_000,
        "max_elapsed_minutes": 10,
        "protected_paths": (".hoh", ".git"),
    }
    values.update(overrides)
    return HarnessConfig(**values)  # type: ignore[arg-type]


def build_services(
    project: Path,
    backend,
    adapter: RecordingAdapter,
    *,
    config: HarnessConfig | None = None,
    policy: StopPolicy | None = None,
    orchestrator_options: Mapping[str, object] | None = None,
) -> Services:
    resolved_config = config or config_for(project)
    git = GitService(project)
    store = StateStore(project)
    skill_root = Path(__file__).parents[2] / "src" / "hoh" / "resources" / "skills"
    orchestrator = HoHOrchestrator(
        resolved_config,
        backend,
        adapter,
        git,
        store,
        SkillRegistry.load(skill_root),
        policy or StopPolicy(resolved_config),
        **dict(orchestrator_options or {}),
    )
    return Services(orchestrator, backend, adapter, git)


def orchestrator_fixture(
    tmp_path: Path, *, crash_on_first_check: bool = False
) -> tuple[Path, Services]:
    project = initialized_product(tmp_path)
    backend = FakeAgentBackend(
        [
            FakeResponse(plan()),
            FakeResponse(
                developer_response(),
                on_run=developer_change,
            ),
            FakeResponse(qa_response),
        ]
    )
    adapter = RecordingAdapter(crash_on_first_check=crash_on_first_check)
    return project, build_services(project, backend, adapter)


def crashed_after_candidate_fixture(tmp_path: Path) -> tuple[Path, Services]:
    project, services = orchestrator_fixture(tmp_path, crash_on_first_check=True)
    try:
        services.orchestrator.run(max_loops=1)
    except RuntimeError as error:
        assert str(error) == "simulated crash after candidate"
    else:  # pragma: no cover - guards the fixture contract
        raise AssertionError("fixture did not crash")
    services.backend.requests.clear()
    return project, services


def count_candidate_commits(project: Path) -> int:
    messages = run_git(project, "log", "--format=%s").splitlines()
    return sum(message.startswith("feat(loop-") for message in messages)
