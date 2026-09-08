# HoH Orchestrator Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the simulated VoidKnight loop runner with a reusable, evidence-grounded HoH-inspired Python orchestrator that performs genuine sequential Planner, Developer, and frozen-candidate QA invocations.

**Architecture:** An installable Python CLI operates on a separate product Git repository. A host-owned state machine invokes a pluggable agent backend, enforces role sandboxes and protected paths, creates candidate commits and detached QA worktrees, runs deterministic product adapters, normalizes candidate-bound evidence, and stops on verified completion or bounded failure conditions.

**Tech Stack:** Python 3.10+, standard library (`argparse`, `dataclasses`, `enum`, `hashlib`, `json`, `subprocess`, `tomllib`), `jsonschema`, `pytest`, Git, Codex CLI non-interactive mode, optional Godot 4.x.

**Spec:** `docs/superpowers/specs/2026-09-07-hoh-orchestrator-design.md`

## Global Constraints

- The current remote head remains recoverable from branch `legacy/voidknight` and tag `legacy-voidknight-2026-09-07`.
- Work occurs on `codex/hoh-rewrite`; do not force-push or use destructive Git reset.
- Planner and QA always use `read-only`; Developer alone uses `workspace-write`.
- The host owns state transitions, schemas, Git operations, hashes, retries, receipts, and stop decisions.
- A run defaults to 12 loops, at most three priorities, one role retry, three no-progress loops, three same-blocker loops, and a 45-minute role timeout.
- A run defaults to a 5,000,000-token total budget and 480 elapsed minutes.
- Missing executables, timeouts, malformed output, or missing evidence never become a pass.
- Core tests run without network, model quota, Codex authentication, or Godot.
- The public README calls the project HoH-inspired and never claims an exact reproduction of the unreleased runtime.
- Do not add a license or claim MIT licensing unless the owner separately selects a license.
- Use `python -m pytest` for test commands so the active interpreter is explicit.
- Test factory names shown in snippets are local helpers, not production APIs. Define each helper in the same test module unless that task lists a dedicated helper file; each helper must create exactly the state described by its name and assertions.

## Locked File Map

| Path | Responsibility |
|---|---|
| `src/hoh/models.py` | Shared immutable domain types and enums only |
| `src/hoh/config.py` | TOML loading, validation, initialization, and diagnostics |
| `src/hoh/state/store.py` | Atomic JSON writes, phase journal, run lock |
| `src/hoh/vcs/git.py` | Git command boundary, clean checks, branch and candidate commits |
| `src/hoh/vcs/worktree.py` | Detached QA worktree lifecycle |
| `src/hoh/skills/registry.py` | Versioned TOML-front-matter skill discovery and selection |
| `src/hoh/resources/` | Packaged prompts, schemas, and built-in skill documents |
| `src/hoh/backends/base.py` | `AgentBackend` protocol and request/result contracts |
| `src/hoh/backends/codex_exec.py` | Codex JSONL subprocess execution and usage extraction |
| `src/hoh/backends/fake.py` | Deterministic test backend only |
| `src/hoh/prompts.py` | Role prompt assembly and progressive skill injection |
| `src/hoh/adapters/base.py` | Product adapter protocol |
| `src/hoh/adapters/command.py` | Configured command checks and evidence collection |
| `src/hoh/adapters/godot.py` | Strict Godot diagnostics and default headless check |
| `src/hoh/state/evidence.py` | Candidate-bound evidence validation and normalization |
| `src/hoh/state/issue_ledger.py` | Stable issue lifecycle and regression history |
| `src/hoh/policy.py` | Progress, completion, and stopping decisions |
| `src/hoh/reporting.py` | Deterministic status and run-summary rendering |
| `src/hoh/orchestrator.py` | Host-owned loop state machine and resume behavior |
| `src/hoh/cli.py` | CLI parsing and dependency assembly only |

---

### Task 1: Preserve Legacy State and Bootstrap the Python Package

**Files:**
- Delete: `.gameloop/`, `build/`, `game/`, `hoh_engine/`, `__pycache__/`, `hoh_runner.py`, `run_batch_loops.py`, `PRD.md`, `README.md`
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/hoh/__init__.py`
- Create: `src/hoh/cli.py`
- Create: `tests/test_package.py`

**Interfaces:**
- Consumes: Existing Git refs `legacy/voidknight` and `legacy-voidknight-2026-09-07`.
- Produces: Installable distribution `hoh-game-studio`; console command `hoh`; `hoh.cli.main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Verify both recovery refs point to the pre-rewrite commit**

Run:

```powershell
$expected = git rev-parse origin/main
if ((git rev-parse legacy/voidknight) -ne $expected) { throw 'legacy branch mismatch' }
if ((git rev-parse legacy-voidknight-2026-09-07) -ne $expected) { throw 'legacy tag mismatch' }
```

Expected: no output and exit code 0.

- [ ] **Step 2: Write the failing package smoke test**

```python
from hoh import __version__
from hoh.cli import main


def test_package_exports_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_without_command_prints_help(capsys) -> None:
    assert main([]) == 2
    assert "usage: hoh" in capsys.readouterr().err
```

- [ ] **Step 3: Remove the legacy working-tree payload after the ref check**

Run from the repository root with the explicit tracked targets:

```powershell
git rm -r -- .gameloop build game hoh_engine __pycache__ hoh_runner.py run_batch_loops.py PRD.md README.md
```

Expected: only the listed legacy paths are staged for deletion; `docs/` remains.

- [ ] **Step 4: Add packaging and the minimal CLI**

`pyproject.toml` must contain:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "hoh-game-studio"
version = "0.1.0"
description = "Evidence-grounded Harness-of-Harness inspired orchestrator"
requires-python = ">=3.10"
dependencies = ["jsonschema>=4.23,<5"]

[project.optional-dependencies]
test = ["pytest>=8.3,<9"]

[project.scripts]
hoh = "hoh.cli:entrypoint"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
hoh = [
  "resources/prompts/*.md",
  "resources/schemas/*.json",
  "resources/skills/core/*.md",
  "resources/skills/godot/*.md",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

`src/hoh/__init__.py`:

```python
__version__ = "0.1.0"
```

`src/hoh/cli.py`:

```python
from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="hoh")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_usage(__import__("sys").stderr)
    return 2


def entrypoint() -> None:
    raise SystemExit(main())
```

`.gitignore` must include `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.coverage`, `.godot/`, `dist/`, and `*.egg-info/`.

- [ ] **Step 5: Install and verify the smoke test**

Run:

```powershell
python -m pip install -e '.[test]'
python -m pytest tests/test_package.py -q
```

Expected: 2 tests pass.

- [ ] **Step 6: Commit the recoverable repository reset**

```powershell
git add -- .gitignore pyproject.toml src tests
git commit -m "chore: bootstrap HoH orchestrator package"
```

---

### Task 2: Define Domain Models and Strict Configuration

**Files:**
- Create: `src/hoh/models.py`
- Create: `src/hoh/config.py`
- Create: `tests/test_config.py`
- Modify: `src/hoh/cli.py`

**Interfaces:**
- Consumes: `hoh.cli.main` from Task 1.
- Produces: `Role`, `Sandbox`, `Phase`, `Diagnostic`, `CheckResult`, `CheckBundle`, `HarnessConfig`; `load_config(project: Path) -> HarnessConfig`; `initialize_project(project: Path, adapter: str, model: str, reasoning_effort: str) -> Path`; `doctor(config: HarnessConfig) -> tuple[Diagnostic, ...]`.

- [ ] **Step 1: Write failing initialization and validation tests**

```python
from pathlib import Path

import pytest

from hoh.config import ConfigError, initialize_project, load_config


def git_init(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)


def test_init_writes_explicit_reproducible_config(tmp_path: Path) -> None:
    git_init(tmp_path)
    initialize_project(tmp_path, "command", "test-model", "high")
    config = load_config(tmp_path)
    assert config.model == "test-model"
    assert config.reasoning_effort == "high"
    assert config.max_loops == 12
    assert config.protected_paths == (".hoh", ".git")
    assert config.max_total_tokens == 5_000_000
    assert config.max_elapsed_minutes == 480


def test_init_rejects_missing_model(tmp_path: Path) -> None:
    git_init(tmp_path)
    with pytest.raises(ConfigError, match="model is required"):
        initialize_project(tmp_path, "command", "", "high")
```

- [ ] **Step 2: Run the tests and confirm the missing modules fail**

Run: `python -m pytest tests/test_config.py -q`

Expected: collection fails because `hoh.config` does not exist.

- [ ] **Step 3: Implement immutable types and configuration**

Define exact enums and records in `models.py`:

```python
class Role(str, Enum):
    PLANNER = "planner"
    DEVELOPER = "developer"
    QA = "qa"


class Sandbox(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class Phase(str, Enum):
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    BASELINE = "baseline"
    DEVELOPMENT = "development"
    CANDIDATE = "candidate"
    CHECKING = "checking"
    QA = "qa"
    CLOSURE = "closure"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Literal["info", "warning", "blocked"]
    message: str


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: Literal["pass", "fail", "blocked"]
    summary: str
    artifact_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckBundle:
    adapter: str
    status: Literal["pass", "fail", "blocked"]
    results: tuple[CheckResult, ...]
    artifact_paths: tuple[str, ...] = ()
```

Define `HarnessConfig` with exact fields from the spec, plus `project`, `adapter`, `codex_bin`, `protected_paths`, `role_timeout_minutes`, `max_total_tokens`, `max_elapsed_minutes`, and `adapter_options`. Validate positive integers, known adapters, nonempty model, and nonempty reasoning effort. `initialize_project` creates `.hoh/config.toml`, `.hoh/prd.md`, `.hoh/requirements.json`, `.hoh/issue-ledger.json`, and `.hoh/runs/` without overwriting existing files. The initial requirement registry is `{"schema_version": 1, "claims": []}`; `doctor` returns a blocked diagnostic until it contains at least one claim with `"required": true`.

- [ ] **Step 4: Add `init` and `doctor` parser definitions**

`build_parser()` must create required subcommands. `init` requires `--adapter`, `--model`, and `--reasoning-effort`; `doctor` accepts `--project` with default `.`. Keep command handlers in `config.py` for this task; later tasks will move dependency assembly into `cli.py`.

- [ ] **Step 5: Run focused and full tests**

Run:

```powershell
python -m pytest tests/test_config.py -q
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/hoh/models.py src/hoh/config.py src/hoh/cli.py tests/test_config.py
git commit -m "feat: add strict HoH project configuration"
```

---

### Task 3: Add Atomic State, Phase Journal, and Product Lock

**Files:**
- Create: `src/hoh/state/__init__.py`
- Create: `src/hoh/state/store.py`
- Create: `tests/state/test_store.py`

**Interfaces:**
- Consumes: `Phase` from Task 2.
- Produces: `atomic_write_json(path: Path, payload: Mapping[str, object]) -> None`; `StateStore.create_run(run_id: str, start_sha: str) -> Path`; `StateStore.phase_state(run_id: str, loop_index: int) -> dict`; `StateStore.complete_phase(...) -> None`; `StateStore.first_incomplete_phase(...) -> Phase`; `RunLock.acquire()` and `RunLock.release()`.

- [ ] **Step 1: Write failing atomicity, idempotency, and lock tests**

```python
def test_complete_phase_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.create_run("run-1", "abc123")
    store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "p.json"})
    first = store.phase_state("run-1", 1)
    store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "p.json"})
    assert store.phase_state("run-1", 1) == first
    assert first["completed"]["planning"]["idempotency_key"] == "run-1:1:planning"


def test_second_lock_is_rejected(tmp_path: Path) -> None:
    first = RunLock(tmp_path / ".hoh" / "lock")
    second = RunLock(tmp_path / ".hoh" / "lock")
    first.acquire()
    with pytest.raises(RunLockedError):
        second.acquire()
    first.release()
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/state/test_store.py -q`

Expected: import failure for `hoh.state.store`.

- [ ] **Step 3: Implement atomic writes and phase ordering**

Use `tempfile.NamedTemporaryFile(delete=False, dir=path.parent)`, `flush()`, `os.fsync()`, and `os.replace()`. Store completed phases in the fixed `Phase` order from Task 2. If an idempotency key already exists with different payload content, raise `StateConflictError`; identical repetition is a no-op.

Implement the lock with exclusive file creation mode `"x"`. The file contains PID and UTC timestamp. Do not silently break a stale lock; `doctor` will report it and the user may remove it after verifying that no run is active.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/state/test_store.py -q`

Expected: atomicity, idempotency, and lock tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/hoh/state tests/state
git commit -m "feat: add crash-safe HoH state journal"
```

---

### Task 4: Enforce Git Branch, Candidate, and Frozen Worktree Boundaries

**Files:**
- Create: `src/hoh/vcs/__init__.py`
- Create: `src/hoh/vcs/git.py`
- Create: `src/hoh/vcs/worktree.py`
- Create: `tests/vcs/test_git.py`
- Create: `tests/vcs/test_worktree.py`

**Interfaces:**
- Consumes: product repository path.
- Produces: `GitService.assert_clean()`, `head_sha() -> str`, `create_run_branch(run_id: str) -> str`, `changed_paths(base_sha: str) -> tuple[str, ...]`, `snapshot_paths(paths: tuple[str, ...]) -> dict[str, str]`, `assert_snapshot_unchanged(snapshot: Mapping[str, str])`, `commit_candidate(loop_index: int, summary: str) -> str`, `commit_evidence(loop_index: int, paths: tuple[Path, ...]) -> str`, `rev_parse_in(worktree: Path, revision: str) -> str`, `current_branch_in(worktree: Path) -> str`; `QaWorktree.create(candidate_sha: str) -> Path`, `QaWorktree.remove() -> None`.

- [ ] **Step 1: Write failing Git boundary tests**

```python
def test_candidate_and_detached_worktree_are_bound(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    (repo / "product.txt").write_text("changed", encoding="utf-8")
    candidate = git.commit_candidate(1, "change product")
    with QaWorktree(git, repo / ".hoh" / "tmp" / "qa-1") as frozen:
        assert git.rev_parse_in(frozen, "HEAD") == candidate
        assert git.current_branch_in(frozen) == ""


def test_protected_change_is_rejected(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    (repo / ".hoh").mkdir()
    (repo / ".hoh" / "owned.json").write_text("{}", encoding="utf-8")
    snapshot = git.snapshot_paths((".hoh", ".git"))
    (repo / ".hoh" / "owned.json").write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(ProtectedPathError, match=r"\.hoh"):
        git.assert_snapshot_unchanged(snapshot)
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/vcs -q`

Expected: imports fail for missing VCS modules.

- [ ] **Step 3: Implement a checked Git command wrapper**

All Git subprocesses use argument lists, `cwd=repo`, `text=True`, `capture_output=True`, and `check=False`. Convert nonzero exits to `GitError` containing the command name and sanitized stderr. Never invoke a shell.

Use branch `hoh/run-<run-id>`, candidate commit message `feat(loop-<four digits>): <summary>`, evidence commit message `test(loop-<four digits>): record candidate evidence`, and host-controlled authors `hoh-developer[bot] <developer@hoh.local>` and `hoh-qa[bot] <qa@hoh.local>`. Stage production changes only after protected-path validation. A candidate commit excludes `.hoh`; the later evidence commit stages only the host-selected state and evidence paths.

Protected-path validation compares a recursive SHA-256 snapshot captured immediately before the Developer invocation with a snapshot captured immediately after it. Host event streams are staged outside the product workspace during the invocation, then imported into `.hoh` only after this comparison, so a Developer change cannot be confused with a host state write. Include tracked, untracked, and deleted protected files in the snapshot.

- [ ] **Step 4: Implement detached worktree cleanup**

Create with `git worktree add --detach <path> <candidate-sha>`. Remove with `git worktree remove --force <path>` only after resolving the absolute path and verifying it is under `<product>/.hoh/tmp/`. Prune after successful removal. Context manager exit always attempts cleanup and reports cleanup failure separately.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/vcs -q`

Expected: candidate, dirty-tree, protected-path, detached-head, and cleanup tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/hoh/vcs tests/vcs
git commit -m "feat: bind QA evidence to frozen Git candidates"
```

---

### Task 5: Implement the Versioned Skill Registry

**Files:**
- Create: `src/hoh/skills/__init__.py`
- Create: `src/hoh/skills/registry.py`
- Create: `src/hoh/resources/skills/core/bounded-planning.md`
- Create: `src/hoh/resources/skills/core/evidence-grounded-qa.md`
- Create: `src/hoh/resources/skills/godot/asset-pipeline.md`
- Create: `src/hoh/resources/skills/godot/ui-ux-polish.md`
- Create: `src/hoh/resources/skills/godot/runtime-testing.md`
- Create: `src/hoh/resources/skills/godot/performance-tuning.md`
- Create: `tests/skills/test_registry.py`

**Interfaces:**
- Consumes: `Role` and adapter ID.
- Produces: immutable `SkillDocument`; `SkillRegistry.load(root: Path) -> SkillRegistry`; `select(role: Role, adapter: str, requested_ids: tuple[str, ...]) -> tuple[SkillDocument, ...]`; `render_bundle(skills) -> str`.

- [ ] **Step 1: Write failing discovery and progressive-disclosure tests**

```python
def test_select_returns_only_matching_skills_in_requested_order(skill_root: Path) -> None:
    registry = SkillRegistry.load(skill_root)
    selected = registry.select(
        Role.QA,
        "godot",
        ("core.evidence-grounded-qa", "godot.runtime-testing"),
    )
    assert [skill.skill_id for skill in selected] == [
        "core.evidence-grounded-qa",
        "godot.runtime-testing",
    ]
    assert all(len(skill.sha256) == 64 for skill in selected)


def test_incompatible_skill_is_rejected(skill_root: Path) -> None:
    registry = SkillRegistry.load(skill_root)
    with pytest.raises(SkillConflictError):
        registry.select(Role.QA, "godot", ("fixture.a", "fixture.b"))
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/skills/test_registry.py -q`

Expected: import failure for the missing registry.

- [ ] **Step 3: Implement TOML front matter and validation**

Every skill starts with this format:

```markdown
+++
id = "core.bounded-planning"
version = "1.0.0"
roles = ["planner"]
adapters = ["*"]
dependencies = []
incompatible = []
+++

# Bounded Planning
```

Parse the delimited metadata using `tomllib`. Reject duplicate IDs, invalid semantic versions, unknown roles, missing dependencies, dependency cycles, incompatible selections, and content without instructions. Hash the normalized complete file bytes with SHA-256.

- [ ] **Step 4: Write the six bounded skill documents**

Each document must state its role, desired outcome, evidence expectations, and prohibitions. Godot skills may guide the agent but must explicitly say they cannot override host permissions, schemas, protected paths, or stopping policy. Asset guidance must require recorded source and license information for external assets.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/skills/test_registry.py -q`

Expected: discovery, role filtering, adapter filtering, dependency, conflict, and hashing tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/hoh/skills src/hoh/resources/skills tests/skills
git commit -m "feat: add versioned progressive skill registry"
```

---

### Task 6: Implement Codex and Fake Agent Backends

**Files:**
- Create: `src/hoh/backends/__init__.py`
- Create: `src/hoh/backends/base.py`
- Create: `src/hoh/backends/codex_exec.py`
- Create: `src/hoh/backends/fake.py`
- Create: `tests/backends/test_codex_exec.py`
- Create: `tests/backends/test_fake.py`

**Interfaces:**
- Consumes: `Role`, `Sandbox`, workspace, prompt, schema, model, reasoning effort, timeout, events path.
- Produces: `AgentRequest`, `AgentUsage`, `AgentResult`; protocol `AgentBackend.run(request: AgentRequest) -> AgentResult`; `CodexExecBackend`; deterministic `FakeAgentBackend`.

- [ ] **Step 1: Write failing command, usage, timeout, and sandbox tests**

```python
def test_developer_command_uses_workspace_write(tmp_path: Path) -> None:
    request = agent_request(tmp_path, Role.DEVELOPER, Sandbox.WORKSPACE_WRITE)
    command = CodexExecBackend("codex").build_command(request)
    assert command[:3] == ["codex", "exec", "--json"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--output-schema") + 1] == str(request.schema_path)
    assert command[-1] == "-"


def test_read_only_role_cannot_request_write_sandbox(tmp_path: Path) -> None:
    request = agent_request(tmp_path, Role.QA, Sandbox.WORKSPACE_WRITE)
    with pytest.raises(BackendProtocolError, match="qa must be read-only"):
        CodexExecBackend("codex").run(request)
```

Add a fixture executable that emits a `turn.completed` JSONL event and final JSON response, plus fixtures for malformed JSONL, nonzero exit, and timeout.

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/backends -q`

Expected: imports fail for missing backend modules.

- [ ] **Step 3: Implement exact backend records**

```python
@dataclass(frozen=True)
class AgentRequest:
    role: Role
    prompt: str
    workspace: Path
    sandbox: Sandbox
    schema_path: Path
    model: str
    reasoning_effort: str
    timeout_seconds: int
    events_path: Path


@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass(frozen=True)
class AgentResult:
    response: dict[str, object]
    usage: AgentUsage
    return_code: int
```

- [ ] **Step 4: Implement `CodexExecBackend` without a shell**

Build:

```text
codex exec --json --ephemeral --model <model> -c model_reasoning_effort="<effort>" --sandbox <sandbox> --output-schema <schema> -
```

Pass the prompt through stdin. Stream every stdout JSON object to `events_path`; parse usage from the final `turn.completed`; extract the final agent JSON object. Redact environment and command values matching `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, or `AUTH`. Raise distinct `BackendTimeout`, `BackendProcessError`, and `BackendProtocolError` exceptions. Never use `--full-auto` or `danger-full-access`.

- [ ] **Step 5: Implement deterministic fake responses**

`FakeAgentBackend` accepts an ordered sequence of `FakeResponse(response, usage, on_run)`. `response` is either a mapping or `Callable[[AgentRequest], Mapping[str, object]]`, allowing QA fixtures to bind their response to the candidate visible in the request. `on_run`, when present, receives the request and may create product changes for integration tests. It records every request and raises `UnexpectedInvocation` if exhausted.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/backends -q`

Expected: command construction, permissions, JSONL, usage, redaction, failure, timeout, and fake ordering tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/hoh/backends tests/backends
git commit -m "feat: execute role-isolated Codex invocations"
```

---

### Task 7: Add Structured Role Schemas and Prompt Rendering

**Files:**
- Create: `src/hoh/resources/schemas/plan.schema.json`
- Create: `src/hoh/resources/schemas/developer-report.schema.json`
- Create: `src/hoh/resources/schemas/evidence.schema.json`
- Create: `src/hoh/resources/schemas/requirements.schema.json`
- Create: `src/hoh/resources/prompts/planner.md`
- Create: `src/hoh/resources/prompts/developer.md`
- Create: `src/hoh/resources/prompts/qa.md`
- Create: `src/hoh/prompts.py`
- Create: `tests/test_prompts.py`
- Create: `tests/schemas/test_schemas.py`

**Interfaces:**
- Consumes: role context mappings and selected `SkillDocument` objects.
- Produces: `PromptRenderer.render(role: Role, context: Mapping[str, object], skills: tuple[SkillDocument, ...]) -> str`; schemas accepted by `jsonschema.Draft202012Validator` and Codex `--output-schema`.

- [ ] **Step 1: Write failing schema tests with valid and invalid payloads**

```python
def test_plan_rejects_more_than_three_priorities(schema_dir: Path) -> None:
    plan = valid_plan()
    plan["priorities"] = [priority(str(i)) for i in range(4)]
    with pytest.raises(jsonschema.ValidationError):
        validate(plan, schema_dir / "plan.schema.json")


def test_verified_record_requires_candidate_bound_artifact(schema_dir: Path) -> None:
    evidence = valid_evidence()
    evidence["verified_records"][0]["execution_records"] = []
    with pytest.raises(jsonschema.ValidationError):
        validate(evidence, schema_dir / "evidence.schema.json")
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/schemas tests/test_prompts.py -q`

Expected: missing schemas and renderer fail.

- [ ] **Step 3: Define exact plan and evidence shapes**

`requirements.schema.json` requires `schema_version` equal to `1` and a `claims` array. Each unique claim record requires `id`, `description`, and `required`; at least one claim must have `required` equal to true before `doctor` permits a run.

`plan.schema.json` requires `iteration`, `objective`, one-to-three `priorities`, `preservation_constraints`, and `acceptance_gate`. Each priority requires `id`, `title`, `implementation_target`, nonempty `acceptance_claims`, and `source_gap_ids`.

`developer-report.schema.json` requires `summary`, `completed_priority_ids`, `self_tests`, and `known_gaps`.

`evidence.schema.json` requires `iteration`, `candidate_sha`, `artifact_tree_sha256`, `qa_status`, `product_complete`, `verified_records`, `gap_records`, and `planner_handoff`. Each execution record requires `type`, `path`, `sha256`, and `observation`. Set `additionalProperties: false` for every object.

- [ ] **Step 4: Write role templates with hard boundaries**

Planner template says planning only, maximum three priorities, public inputs only, blockers and regressions before expansion, and exact schema output.

Developer template says it is the sole production writer, must not change `.hoh` or `.git`, must baseline and retest, must preserve verified behavior, and cannot claim acceptance.

QA template says frozen candidate, read-only, source presence is insufficient, every verified claim needs an execution record, missing evidence is `insufficient_evidence`, and QA cannot repair or declare global completion.

- [ ] **Step 5: Implement deterministic rendering**

`PromptRenderer` replaces named sections `PUBLIC_PRD`, `PROJECT_SUMMARY`, `PREVIOUS_EVIDENCE`, `ISSUE_LEDGER`, `CURRENT_PLAN`, `CHECKS`, and `SKILLS`. Reject a missing required section and unknown braces after rendering. Include each skill ID, version, hash, and content in the selected order.

- [ ] **Step 6: Run tests and commit**

```powershell
python -m pytest tests/schemas tests/test_prompts.py -q
git add src/hoh/resources/schemas src/hoh/resources/prompts src/hoh/prompts.py tests/schemas tests/test_prompts.py
git commit -m "feat: add constrained HoH role contracts"
```

---

### Task 8: Add the Generic Command Product Adapter

**Files:**
- Create: `src/hoh/adapters/__init__.py`
- Create: `src/hoh/adapters/base.py`
- Create: `src/hoh/adapters/command.py`
- Create: `tests/adapters/test_command.py`

**Interfaces:**
- Consumes: project path, validated plan, command adapter options.
- Produces: `ProductAdapter` protocol; `AdapterContext`; `CommandAdapter.doctor`, `summarize`, `baseline`, `check`, and `collect` returning shared model types.

- [ ] **Step 1: Write failing pass, nonzero, timeout, and missing-artifact tests**

```python
def test_missing_required_artifact_fails_candidate(tmp_path: Path) -> None:
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ok')"),),
        required_artifacts=("evidence/telemetry.jsonl",),
        timeout_seconds=5,
    )
    bundle = adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {})
    assert bundle.status == "fail"
    assert bundle.results[-1].check_id == "artifact:evidence/telemetry.jsonl"


def test_timeout_is_failure_not_pass(tmp_path: Path) -> None:
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "import time; time.sleep(2)"),),
        required_artifacts=(),
        timeout_seconds=1,
    )
    assert adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {}).status == "fail"
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/adapters/test_command.py -q`

Expected: missing adapter modules.

- [ ] **Step 3: Implement strict command execution**

Commands are tuples of arguments and never pass through a shell. Capture stdout, stderr, exit code, start/end UTC timestamps, and timeout status into separate output files. A nonzero exit, timeout, missing required artifact, or configured error regex produces `fail`. A missing executable produces `blocked`.

`summarize` returns tracked file counts, language suffix counts, current SHA, and configured entrypoints without embedding file contents. `collect` copies only configured artifact globs into the host output directory and returns relative paths plus SHA-256 hashes.

- [ ] **Step 4: Run tests and commit**

```powershell
python -m pytest tests/adapters/test_command.py -q
git add src/hoh/adapters tests/adapters/test_command.py
git commit -m "feat: add strict command product adapter"
```

---

### Task 9: Add the Strict Godot Adapter

**Files:**
- Create: `src/hoh/adapters/godot.py`
- Create: `tests/adapters/test_godot.py`
- Create: `tests/fixtures/fake_godot.py`
- Create: `examples/minimal-godot/project.godot`
- Create: `examples/minimal-godot/main.gd`
- Create: `examples/minimal-godot/main.tscn`

**Interfaces:**
- Consumes: `AdapterContext`, configured Godot command prefix, project subdirectory, optional replay/test commands, required evidence globs.
- Produces: `GodotAdapter` implementing `ProductAdapter` and check IDs `godot:executable`, `godot:project`, `godot:headless-import`, `godot:runtime-errors`, plus configured replay and evidence checks.

- [ ] **Step 1: Write failing prerequisite and timeout tests**

```python
def test_missing_godot_is_blocked(tmp_path: Path) -> None:
    adapter = GodotAdapter(command=(str(tmp_path / "missing-godot"),), project_subdir=".")
    diagnostics = adapter.doctor(tmp_path)
    assert diagnostics == (
        Diagnostic("godot:executable", "blocked", "Godot executable was not found"),
    )


def test_fake_godot_error_fails(tmp_path: Path, fake_godot: Path) -> None:
    project = copy_minimal_project(tmp_path)
    adapter = GodotAdapter(command=(sys.executable, str(fake_godot)), project_subdir=".")
    bundle = adapter.check(AdapterContext(project, tmp_path / "out"), {})
    assert bundle.status == "fail"
    assert any(result.check_id == "godot:runtime-errors" for result in bundle.results)
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/adapters/test_godot.py -q`

Expected: `GodotAdapter` is missing.

- [ ] **Step 3: Implement executable and project diagnostics**

Resolve the first command-prefix element as an executable without guessing a macOS path. Require `<project_subdir>/project.godot`. Append the default deterministic arguments to the configured prefix:

```text
<godot> --headless --path <project> --editor --quit
```

Apply the command adapter's timeout and missing-executable semantics. Treat `SCRIPT ERROR`, `Parse Error`, `ERROR:`, crashes, missing required evidence, and nonzero exit as failure. A timeout is failure.

- [ ] **Step 4: Add configurable gameplay evidence hooks**

Accept additional argument-list commands for test scenes and input replay. Accept required evidence globs for telemetry, screenshots, and replay outputs. Every required glob must match at least one file. The minimal example only demonstrates clean headless boot; it does not fabricate gameplay evidence.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m pytest tests/adapters/test_godot.py -q
git add src/hoh/adapters/godot.py tests/adapters/test_godot.py tests/fixtures examples/minimal-godot
git commit -m "feat: add strict Godot product adapter"
```

---

### Task 10: Normalize Evidence and Maintain the Issue Ledger

**Files:**
- Create: `src/hoh/state/evidence.py`
- Create: `src/hoh/state/issue_ledger.py`
- Create: `tests/state/test_evidence.py`
- Create: `tests/state/test_issue_ledger.py`

**Interfaces:**
- Consumes: raw schema-valid QA output, required-claim registry, candidate SHA/tree hash, adapter manifest.
- Produces: `EvidenceNormalizer.normalize(...) -> dict[str, object]`; `IssueLedger.load()`, `apply(evidence, loop_index)`, `summary()`.

- [ ] **Step 1: Write failing candidate and artifact binding tests**

```python
def test_evidence_for_other_candidate_is_rejected(tmp_path: Path) -> None:
    raw = valid_evidence(candidate_sha="wrong")
    with pytest.raises(EvidenceBindingError, match="candidate_sha"):
        EvidenceNormalizer(tmp_path).normalize(raw, "expected", "a" * 64, {})


def test_tampered_artifact_hash_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "frame.png"
    artifact.write_bytes(b"frame")
    raw = valid_evidence_with_record("frame.png", "0" * 64)
    with pytest.raises(EvidenceBindingError, match="sha256"):
        EvidenceNormalizer(tmp_path).normalize(raw, "abc", "a" * 64, {})
```

- [ ] **Step 2: Write failing regression lifecycle test**

```python
def test_verified_claim_that_later_gaps_becomes_regressed(tmp_path: Path) -> None:
    ledger = IssueLedger(tmp_path / "issue-ledger.json")
    ledger.apply(evidence_with_gap("player-moves"), 1)
    ledger.apply(evidence_with_verified("player-moves"), 2)
    ledger.apply(evidence_with_gap("player-moves"), 3)
    issue = ledger.load()["issues"][0]
    assert issue["status"] == "regressed"
    assert [event["status"] for event in issue["history"]] == ["open", "closed", "regressed"]
```

- [ ] **Step 3: Run and verify failure**

Run: `python -m pytest tests/state/test_evidence.py tests/state/test_issue_ledger.py -q`

Expected: missing normalization and ledger classes.

- [ ] **Step 4: Implement normalization and derived issue counts**

Recalculate SHA-256 for every cited file under the host evidence root, reject traversal outside the root, and reject paths absent from the adapter manifest. Replace no fields supplied by QA except deterministic ordering and host metadata. Ignore QA's global completion claim when calculating host completion. Set host `product_complete` to false when any claim marked required in `.hoh/requirements.json` lacks a verified record on this candidate, any gap is blocker/major, or deterministic checks did not pass.

Use claim ID as stable issue identity. Status transitions are `open -> closed -> regressed -> closed`. Derive all summary counts from issue entries on every write. Preserve loop, candidate, evidence path, and observation in history.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m pytest tests/state/test_evidence.py tests/state/test_issue_ledger.py -q
git add src/hoh/state/evidence.py src/hoh/state/issue_ledger.py tests/state
git commit -m "feat: bind QA evidence and track regressions"
```

---

### Task 11: Implement Progress, Completion, and Stop Policy

**Files:**
- Create: `src/hoh/policy.py`
- Create: `tests/test_policy.py`

**Interfaces:**
- Consumes: `HarnessConfig`, loop histories, latest normalized evidence, deterministic check status, issue summary, elapsed seconds, token total.
- Produces: `ProgressSnapshot`; `StopDecision(should_stop: bool, terminal_status: str | None, reason: str)`; `StopPolicy.evaluate(...) -> StopDecision`.

- [ ] **Step 1: Write failing precedence and three-strike tests**

```python
def test_verified_completion_wins_before_budget_stop() -> None:
    decision = policy().evaluate(history=complete_history_at_loop_12())
    assert decision == StopDecision(True, "complete", "all required claims verified")


def test_same_blocker_three_times_stops() -> None:
    history = [gap_loop("build:blocker") for _ in range(3)]
    assert policy().evaluate(history).terminal_status == "blocked"


def test_two_no_progress_loops_continue() -> None:
    history = [verified_loop({"a"}), verified_loop({"a"})]
    assert policy().evaluate(history).should_stop is False
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_policy.py -q`

Expected: missing policy module.

- [ ] **Step 3: Implement deterministic precedence**

Evaluate in this order: verified completion; explicit cancellation; unrecoverable infrastructure/protocol block; three same blockers; three no-progress loops; token budget; elapsed-time budget; loop budget; continue. Progress is an increase in verified required claim IDs, closure of an existing issue, or successful evidence for an acceptance claim not previously supported. New files or agent-reported completion do not count.

- [ ] **Step 4: Run tests and commit**

```powershell
python -m pytest tests/test_policy.py -q
git add src/hoh/policy.py tests/test_policy.py
git commit -m "feat: enforce bounded continual-development policy"
```

---

### Task 12: Render Deterministic Status and Run Reports

**Files:**
- Create: `src/hoh/reporting.py`
- Create: `tests/test_reporting.py`

**Interfaces:**
- Consumes: persisted run state, receipts, policy decision, issue ledger.
- Produces: `build_status(...) -> dict[str, object]`; `render_run_summary(...) -> str`; `write_run_summary(run_dir: Path, summary: str) -> Path`.

- [ ] **Step 1: Write the failing golden report test**

```python
def test_report_contains_candidate_cost_and_resume(tmp_path: Path) -> None:
    rendered = render_run_summary(report_fixture())
    assert "Status: blocked" in rendered
    assert "Best candidate: abc123" in rendered
    assert "Total tokens: 4200" in rendered
    assert "Remaining gaps" in rendered
    assert "hoh resume" in rendered
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_reporting.py -q`

Expected: missing reporting module.

- [ ] **Step 3: Implement deterministic rendering**

Sort claims and gaps by stable ID. Include run ID, terminal status, reason, start SHA, current and best candidate, completed loops, role attempts, token totals by role, elapsed time, verified claims, remaining gaps, infrastructure/protocol failures, skill IDs/hashes, and exact resume or merge guidance. Render from structured state only; do not ask a model to summarize the run.

- [ ] **Step 4: Run tests and commit**

```powershell
python -m pytest tests/test_reporting.py -q
git add src/hoh/reporting.py tests/test_reporting.py
git commit -m "feat: add auditable HoH run reports"
```

---

### Task 13: Assemble the Orchestrator and Crash-Safe Resume

**Files:**
- Create: `src/hoh/orchestrator.py`
- Create: `tests/orchestrator/test_run.py`
- Create: `tests/orchestrator/test_resume.py`
- Create: `tests/orchestrator/helpers.py`

**Interfaces:**
- Consumes: `HarnessConfig`, `AgentBackend`, `ProductAdapter`, `GitService`, `StateStore`, `SkillRegistry`, `StopPolicy`.
- Produces: `HoHOrchestrator.run(max_loops: int | None = None) -> dict[str, object]`; `HoHOrchestrator.resume() -> dict[str, object]`.

- [ ] **Step 1: Write the failing full-loop test**

```python
def test_one_loop_calls_roles_in_order_and_freezes_qa(tmp_path: Path) -> None:
    project, services = orchestrator_fixture(tmp_path)
    result = services.orchestrator.run(max_loops=1)
    requests = services.backend.requests
    assert [request.role for request in requests] == [Role.PLANNER, Role.DEVELOPER, Role.QA]
    assert [request.sandbox for request in requests] == [
        Sandbox.READ_ONLY,
        Sandbox.WORKSPACE_WRITE,
        Sandbox.READ_ONLY,
    ]
    assert requests[2].workspace != project
    assert result["loops_completed"] == 1
    assert (project / ".hoh" / "runs" / result["run_id"]).exists()
```

The fake Developer response uses `on_run` to modify `product.txt`; the fake adapter writes a real evidence artifact; the fake QA response cites that artifact and exact candidate SHA.

- [ ] **Step 2: Write the failing resume test**

```python
def test_resume_does_not_repeat_completed_developer(tmp_path: Path) -> None:
    project, services = crashed_after_candidate_fixture(tmp_path)
    original_candidate = services.git.head_sha()
    result = services.orchestrator.resume()
    assert [request.role for request in services.backend.requests] == [Role.QA]
    assert result["current_candidate"] == original_candidate
    assert count_candidate_commits(project) == 1
```

- [ ] **Step 3: Run and verify failure**

Run: `python -m pytest tests/orchestrator -q`

Expected: missing orchestrator module.

- [ ] **Step 4: Implement phase-by-phase orchestration**

Each phase first checks the journal idempotency key, executes only if incomplete, writes outputs atomically, validates them, and then marks the phase complete. Use one repair retry only for `BackendTimeout`, `BackendProcessError`, or schema-invalid role output. Retain failed attempt events with `attempt-01` and `attempt-02` filenames.

Immediately before Developer, snapshot protected paths and stage its JSONL event stream outside the product workspace. Immediately after Developer, reject protected-path changes, then import the host-owned event stream and commit production files only. Create the QA worktree only after candidate metadata is durable. Run adapter checks before QA and include their public records in the QA prompt. Normalize QA evidence before applying the ledger. Commit only the selected `.hoh` plan, events, candidate metadata, checks, evidence, receipt, and issue-ledger paths in a separate evidence commit. Remove the disposable worktree in `finally` without deleting retained evidence.

- [ ] **Step 5: Implement host-owned loop continuation**

After closure, compute progress and call `StopPolicy`. If continuing, increment loop index and invoke a fresh Planner. If stopping, write the summary and release the product lock. On exception, persist a classified terminal or resumable state before releasing the lock.

- [ ] **Step 6: Run focused and full tests**

```powershell
python -m pytest tests/orchestrator -q
python -m pytest -q
```

Expected: full-loop, failure, retry, cleanup, candidate binding, multi-loop, stopping, and resume tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/hoh/orchestrator.py tests/orchestrator
git commit -m "feat: orchestrate evidence-grounded HoH loops"
```

---

### Task 14: Complete the CLI and Offline End-to-End Acceptance Tests

**Files:**
- Modify: `src/hoh/cli.py`
- Modify: `src/hoh/config.py`
- Create: `tests/cli/test_cli.py`
- Create: `tests/e2e/test_offline_run.py`

**Interfaces:**
- Consumes: all services from Tasks 2-13.
- Produces: functional `hoh init`, `doctor`, `run`, `status`, `resume`, `report`, and `skills list` commands; dependency factory `build_services(config, backend_name="codex-exec")`.

- [ ] **Step 1: Write failing CLI exit-code tests**

```python
def test_doctor_returns_blocked_exit_code(tmp_path: Path, capsys) -> None:
    initialized_godot_project_without_godot(tmp_path)
    assert main(["doctor", "--project", str(tmp_path)]) == 3
    assert "Godot executable was not found" in capsys.readouterr().out


def test_status_json_is_machine_readable(tmp_path: Path, capsys) -> None:
    completed_fixture(tmp_path)
    assert main(["status", "--project", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
```

- [ ] **Step 2: Write the failing offline end-to-end test**

The test invokes `main(["run", ...])` through an injected `FakeAgentBackend`, uses a temporary product Git repository and `CommandAdapter`, and asserts one candidate commit, one evidence receipt, exact role order, status output, and report output.

- [ ] **Step 3: Run and verify failure**

Run: `python -m pytest tests/cli tests/e2e/test_offline_run.py -q`

Expected: missing commands and dependency assembly fail.

- [ ] **Step 4: Implement command handlers and stable exit codes**

Use exit codes: `0` success or verified completion; `2` usage/configuration error; `3` blocked prerequisite; `4` candidate failure or bounded incomplete run; `5` protocol failure; `130` cancellation. Print concise human output to stdout and errors to stderr. `status --json` emits only JSON.

`build_services` resolves `resources/prompts/`, `resources/schemas/`, and `resources/skills/` with `importlib.resources.files("hoh")`; selects `CodexExecBackend` in production and permits explicit fake injection only through Python tests, not a public CLI flag.

- [ ] **Step 5: Run the complete offline suite**

Run:

```powershell
python -m pytest -q
hoh --help
```

Expected: all tests pass; help lists all seven command groups.

- [ ] **Step 6: Commit**

```powershell
git add src/hoh/cli.py src/hoh/config.py tests/cli tests/e2e/test_offline_run.py
git commit -m "feat: expose complete HoH orchestration CLI"
```

---

### Task 15: Document, Verify, Preserve Legacy Refs Remotely, and Publish

**Files:**
- Create: `README.md`
- Create: `docs/methodology.md`
- Create: `docs/evidence-format.md`
- Create: `.github/workflows/tests.yml`
- Create: `tests/e2e/test_real_codex.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: completed CLI and acceptance suite.
- Produces: public installation and usage documentation, paper-to-implementation mapping, opt-in real Codex smoke test, Windows CI, published legacy refs and rewrite branch/main.

- [ ] **Step 1: Add an opt-in real Codex smoke test**

```python
@pytest.mark.skipif(
    os.environ.get("HOH_REAL_CODEX_E2E") != "1",
    reason="set HOH_REAL_CODEX_E2E=1 to consume Codex quota",
)
def test_real_codex_produces_three_role_attempts(tmp_path: Path) -> None:
    project = create_tiny_product(tmp_path)
    result = run_real_single_loop(project)
    assert result["loops_completed"] == 1
    assert result["role_attempts"] == {"planner": 1, "developer": 1, "qa": 1}
    assert result["current_candidate"]
```

Keep this excluded from normal CI. It must use a tiny text product and one loop, not Godot.

Define `create_tiny_product` in the same test module to initialize and commit a repository containing `product.txt`, `.hoh/prd.md`, `.hoh/requirements.json` with one required claim, and command-adapter configuration. Define `run_real_single_loop` there to load the public service factory with `CodexExecBackend`, override `max_loops` to one, and call `HoHOrchestrator.run`.

- [ ] **Step 2: Write accurate public documentation**

README must include: HoH-inspired disclaimer; architecture; prerequisites; installation; `init`, `doctor`, `run`, `status`, `resume`, and `report` examples; sandbox table; evidence guarantees; stopping defaults; Godot configuration; real-test opt-in warning; legacy branch link; and limitations. Do not say the full paper was reproduced.

`docs/methodology.md` maps artifact state, evidence state, Planner, Developer, QA, warm start, bounded objective, and independent acceptance to the paper and identifies our engineering additions.

`docs/evidence-format.md` documents every plan, developer report, candidate, checks, evidence, receipt, issue, and summary field with one valid example.

- [ ] **Step 3: Add Windows and Linux offline CI**

`.github/workflows/tests.yml` uses `actions/checkout@v5`, `actions/setup-python@v6`, Python 3.10 and 3.13, `python -m pip install -e '.[test]'`, and `python -m pytest -q`. It does not expose a Codex credential and does not run real Codex or Godot E2E tests.

- [ ] **Step 4: Run final verification**

Run:

```powershell
python -m pytest -q
python -m pip check
hoh --help
hoh skills list
git diff --check origin/main...HEAD
git status --short
```

Expected: all offline tests pass, dependency check passes, commands return successfully, diff check is clean, and the worktree is clean after the documentation commit.

- [ ] **Step 5: Commit documentation and CI**

```powershell
git add README.md docs/methodology.md docs/evidence-format.md .github/workflows/tests.yml tests/e2e/test_real_codex.py pyproject.toml
git commit -m "docs: publish HoH orchestrator workflow"
```

- [ ] **Step 6: Push recovery refs before the rewrite**

Run:

```powershell
git push origin refs/heads/legacy/voidknight:refs/heads/legacy/voidknight
git push origin refs/tags/legacy-voidknight-2026-09-07:refs/tags/legacy-voidknight-2026-09-07
```

Expected: both refs appear on `louisfghbvc/hoh-game-studio`. If authentication fails, stop without changing remote `main` and report the exact login requirement.

- [ ] **Step 7: Push the verified rewrite branch**

Run:

```powershell
git push -u origin codex/hoh-rewrite
```

Expected: the branch is visible remotely and contains the full verified history.

- [ ] **Step 8: Update remote `main` without force-pushing**

Only after Steps 4, 6, and 7 succeed:

```powershell
git switch -C main origin/main
git merge --no-ff codex/hoh-rewrite -m "release: replace simulated loops with HoH orchestrator"
git push origin main
git switch codex/hoh-rewrite
```

Expected: remote `main` contains a normal merge commit; legacy branch and tag remain accessible; no force push occurred.

## Final Verification Checklist

- [ ] `python -m pytest -q` passes from a clean checkout.
- [ ] No normal test requires Codex credentials, network, or Godot.
- [ ] Role sandbox assertions cover Planner, Developer, and QA.
- [ ] Missing Godot and timeout tests prove they cannot pass.
- [ ] Evidence tampering and candidate mismatch tests fail closed.
- [ ] Resume test proves Developer and candidate commit are not duplicated.
- [ ] README language is HoH-inspired, not exact or 100% faithful.
- [ ] `legacy/voidknight` and `legacy-voidknight-2026-09-07` exist remotely before `main` changes.
- [ ] Remote `main` is updated without force-push.
