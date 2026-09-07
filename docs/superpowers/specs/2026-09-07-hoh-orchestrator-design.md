# HoH Orchestrator Rewrite Design

**Date:** 2026-09-07  
**Repository:** `louisfghbvc/hoh-game-studio`  
**Target branch:** `codex/hoh-rewrite`  
**Status:** Approved design, pending implementation plan

## 1. Context

The current repository presents a Godot game and a Python runner as a faithful Harness-of-Harness (HoH) reproduction. The runner does not invoke a coding agent for planning, development, or QA. Its planner copies a caller-provided summary into a receipt, its developer records hard-coded paths without modifying the product, and its quality gate treats a missing Godot executable and a timeout as success. The existing loop history therefore cannot serve as evidence of autonomous HoH development.

The rewrite will replace the default branch implementation with a real, reusable HoH-inspired orchestrator. The existing VoidKnight project and history will remain recoverable from branch `legacy/voidknight` and tag `legacy-voidknight-2026-09-07`. The rewrite will not force-push or erase Git history.

The method is based on *Harness-of-Harness: Multi-Day Autonomous Software Development with Continual Improvement* ([arXiv:2609.01481](https://arxiv.org/abs/2609.01481)). Because the authors have not released HoH-lite or their complete private runtime, this project will describe itself as an evidence-grounded, HoH-inspired implementation rather than an exact reproduction.

## 2. Goals

The first implementation milestone will:

1. Provide a reusable Python CLI that can orchestrate software projects outside the harness repository.
2. Execute each loop as three fresh, sequential Codex invocations: Planner, Developer, and QA Tester.
3. Give Planner and QA read-only access and Developer workspace-write access.
4. Carry both artifact state and normalized evidence across loop boundaries.
5. Bind every QA claim to a frozen Git candidate and retained execution records.
6. Support deterministic project adapters, beginning with a generic command adapter and a Godot adapter.
7. Stop safely on completion, budget exhaustion, repeated lack of progress, repeated blockers, protocol violations, or infrastructure failures.
8. Resume interrupted work without repeating completed phases.
9. Retain auditable prompts, skill versions, process events, hashes, token usage, checks, and decisions.
10. Provide sufficient automated tests to distinguish real execution from fabricated receipts.

## 3. Non-goals

The first milestone will not:

- Recreate the authors' unreleased private runtime byte-for-byte.
- Build the full Neon Relay game. Neon Relay is the next project and will validate the finished harness.
- Run indefinitely. Every run has explicit loop, time, and retry bounds.
- Merge a completed product branch into `main` automatically.
- Purchase assets, publish games, create releases, or change external accounts.
- Provide a hosted SaaS service, web dashboard, plugin marketplace, or distributed scheduler.
- Include an open-source license unless the repository owner separately selects one. The README will not claim MIT licensing without a `LICENSE` file.

## 4. Architectural Decisions

### 4.1 Repository and product separation

The harness is an installable Python package. A target product is a separate Git repository passed to the CLI. The harness never treats its own source repository as the product unless explicitly invoked against a separate fixture during testing.

Each product stores durable HoH state under `.hoh/`. Production files remain outside `.hoh/`. A run operates on a dedicated `hoh/run-<run-id>` branch and never writes directly to the product's default branch.

### 4.2 Agent backend

The first backend is `CodexExecBackend`, implemented as a subprocess wrapper around `codex exec`. This interface is selected because Codex non-interactive mode supports explicit sandboxes, JSONL event output, and JSON Schema constrained final output. See the official [Codex non-interactive mode documentation](https://developers.openai.com/codex/non-interactive-mode/).

The backend is hidden behind an `AgentBackend` protocol so a future Codex SDK or another compatible coding harness can be added without changing orchestration logic.

Every role is a fresh invocation. No role relies on hidden chat history. The model, reasoning effort, Codex executable version, prompt hash, skill hashes, sandbox, and timeout are materialized in the receipt.

### 4.3 Single-writer boundary

Only Developer receives `workspace-write`. Planner and QA receive `read-only`. Developer is instructed to modify production files only; after invocation, the host rejects the phase if `.hoh/` or other protected paths changed.

The host, not an agent, owns state transitions, receipt creation, hashes, Git commits, worktree creation, deterministic checks, retry counts, and stopping decisions.

## 5. Package Structure

```text
hoh-game-studio/
├── src/hoh/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── orchestrator.py
│   ├── models.py
│   ├── backends/
│   │   ├── base.py
│   │   ├── codex_exec.py
│   │   └── fake.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── command.py
│   │   └── godot.py
│   ├── state/
│   │   ├── store.py
│   │   ├── evidence.py
│   │   ├── issue_ledger.py
│   │   └── receipts.py
│   ├── skills/
│   │   ├── registry.py
│   │   └── loader.py
│   └── vcs/
│       ├── git.py
│       └── worktree.py
├── prompts/
│   ├── planner.md
│   ├── developer.md
│   └── qa.md
├── schemas/
│   ├── plan.schema.json
│   ├── developer-report.schema.json
│   └── evidence.schema.json
├── skills/
│   ├── core/
│   │   ├── bounded-planning.md
│   │   └── evidence-grounded-qa.md
│   └── godot/
│       ├── asset-pipeline.md
│       ├── ui-ux-polish.md
│       ├── runtime-testing.md
│       └── performance-tuning.md
├── examples/
│   └── minimal-godot/
├── tests/
├── pyproject.toml
└── README.md
```

Runtime dependencies will be deliberately small. The CLI uses Python 3.10 or newer, the standard library for process and TOML handling, and `jsonschema` for host-side validation. Test-only dependencies use `pytest`.

## 6. User Interface

The initial CLI surface is:

```text
hoh init --adapter <command|godot> --model <model-id> --reasoning-effort <level>
hoh doctor
hoh run [--max-loops N]
hoh status [--json]
hoh resume
hoh report
hoh skills list
```

`hoh init` requires a Git repository and writes `.hoh/config.toml`, `.hoh/prd.md`, and an empty issue ledger. Model and reasoning effort are explicit required inputs so a run does not silently drift with the user's Codex defaults. Interactive initialization may prompt for them; non-interactive initialization fails if either is absent.

`hoh doctor` checks Git, Codex, model configuration, writable state paths, adapter executable, required commands, and disk space. A missing Godot executable is a blocking diagnostic for a Godot project.

`hoh run` acquires a product-level lock and creates a run branch. `hoh resume` continues the most recent resumable run from the first incomplete phase. `hoh report` creates a human-readable summary from immutable structured records. None of these commands merge into the default branch.

## 7. Product State Layout

```text
.hoh/
├── config.toml
├── prd.md
├── issue-ledger.json
├── best-candidate.json
├── lock
└── runs/
    └── <run-id>/
        ├── run.json
        ├── run-summary.md
        └── loops/
            └── loop-0001/
                ├── phase-state.json
                ├── plan.json
                ├── planner-events.jsonl
                ├── developer-report.json
                ├── developer-events.jsonl
                ├── candidate.json
                ├── checks.json
                ├── qa-events.jsonl
                ├── evidence.json
                └── receipt.json
```

Writes to state files are atomic: write a sibling temporary file, flush it, and replace the destination. `phase-state.json` includes an idempotency key derived from run ID, loop index, and phase name. A product-level lock prevents concurrent runs in the same repository.

## 8. Loop State Machine

Each loop follows this host-owned sequence:

1. **Preflight** validates configuration, repository cleanliness, budgets, protected paths, and adapter availability.
2. **Planning** calls a fresh read-only Planner with the PRD, previous evidence, issue ledger, project summary, and selected planning skills.
3. **Plan validation** validates `plan.json`, enforces at most three related priorities, and allows one repair retry for schema failure.
4. **Development baseline** runs adapter baseline checks and records their results.
5. **Development** calls a fresh workspace-write Developer with the PRD and validated plan.
6. **Diff validation** rejects protected-path edits and records the production diff.
7. **Candidate creation** commits production changes and records the candidate commit SHA and artifact tree hash.
8. **Candidate freeze** creates a detached QA worktree at the candidate SHA.
9. **Deterministic checks** asks the adapter to execute build, boot, tests, replay, telemetry, log, and media collection as configured.
10. **Independent QA** calls a fresh read-only QA invocation against the frozen candidate and retained public records.
11. **Evidence normalization** validates `evidence.json`, verifies every cited path and hash, and updates the issue ledger.
12. **Loop closure** records progress, updates the best verified candidate when justified, writes the immutable receipt, and evaluates stop conditions.

The next Planner receives the PRD, normalized evidence, issue ledger, and a deterministic project summary. It does not receive an unbounded prior conversation or treat the previous plan as a persistent source of truth.

## 9. Role Contracts

### 9.1 Planner

Planner is read-only. It chooses no more than three coherent priorities. Its output contains:

- An objective with an observable product outcome.
- Ordered implementation targets.
- Verified behavior that must be preserved.
- The smallest end-to-end acceptance checks for the increment.
- Referenced gap and issue IDs from previous evidence.

Planner cannot edit, execute production code, declare completion, or access hidden evaluation results.

### 9.2 Developer

Developer is the only production writer. It receives the PRD, plan, baseline results, and relevant skills. It must repair build or runtime blockers before feature expansion, preserve verified behavior, run local checks after meaningful changes, and leave the project runnable.

Developer's final report is informative. The host derives changed paths from Git and never accepts an implementation claim without candidate-bound checks.

### 9.3 QA Tester

QA is read-only and inspects one frozen candidate. It derives checkable claims from the PRD, current plan, and preservation constraints. A claim can be `verified`, `gap`, or `insufficient_evidence`.

Every verified claim must cite one or more retained execution records with path, SHA-256, observation, and candidate SHA. Source-code presence alone does not prove player-visible behavior. QA cannot repair the candidate or unilaterally declare the product complete.

## 10. Skill Registry

Skills are versioned Markdown modules selected by role and adapter. The registry records:

- Stable skill ID and semantic version.
- Applicable roles and adapters.
- Dependencies and incompatible skills.
- Content SHA-256.
- The exact ordered skill set injected into an invocation.

Core v1 skills are bounded planning and evidence-grounded QA. The Godot pack adds asset pipeline guidance, UI/UX polish, runtime testing, and performance tuning. Only skills relevant to the current role and plan are exposed, implementing progressive disclosure rather than loading the entire library into every prompt.

Skill instructions cannot override host permissions, protected paths, schemas, budgets, or stop conditions.

## 11. Adapter Contract

`ProductAdapter` has narrow operations:

- `doctor(project) -> Diagnostic[]`
- `summarize(project) -> ProjectSummary`
- `baseline(project, plan) -> CheckBundle`
- `check(candidate_worktree, plan) -> CheckBundle`
- `collect(candidate_worktree, check_bundle) -> EvidenceManifest`

`CommandAdapter` runs configured commands and collects configured files. It is the portable reference implementation and test fixture.

`GodotAdapter` accepts a configured Godot executable path and commands for headless import, boot, test scenes, deterministic input replay, telemetry collection, and screenshots. It never returns success when the executable is absent, a process times out, an expected record is missing, or runtime error patterns are present. Generated caches and temporary artifacts are written outside the candidate or removed with the disposable QA worktree.

The local machine currently has Codex CLI 0.104.0, Python 3.13.1, Node.js 22.14.0, and Git 2.54.0. Godot is not installed. Godot-specific real E2E verification is therefore an explicit later prerequisite, while adapter failure and contract behavior can be tested with a fake executable.

## 12. Git and Candidate Integrity

The harness refuses to start from a dirty product worktree. It creates `hoh/run-<run-id>` from the selected starting commit. The host creates candidate commits after a successful Developer phase; an agent does not run branch, merge, reset, or push commands.

QA uses a detached, disposable worktree fixed to the candidate SHA. Evidence records include candidate SHA, artifact tree hash, evidence file hashes, and adapter version. Evidence generated for one candidate cannot verify another candidate.

Failed candidates remain in history. Rollback means creating a new branch or commit from a retained best candidate; the harness never uses `git reset --hard` or silently deletes work. Completing a run produces a recommended merge command and report but does not merge automatically.

## 13. Evidence and Issue Semantics

Normalized evidence contains:

- Run and loop identifiers.
- Candidate SHA and artifact tree hash.
- Overall loop status and product completion status.
- Verified records, each with claim ID, claim, observations, file hashes, and preservation requirement.
- Gap records, each with severity, player or user impact, observations, recommended update, and validation requirement.
- Infrastructure and protocol diagnostics, kept separate from product gaps.
- Planner handoff containing preservation constraints, update targets, and validation requirements.

Issue identity is stable across loops. A previously verified issue that fails on a later candidate becomes `regressed` and retains both the earlier verification and later failure records. Issue counts are derived from issue entries and cannot be supplied by an agent.

## 14. Failure, Retry, and Resume Policy

Default limits are:

```toml
max_loops = 12
max_priorities_per_loop = 3
max_role_retries = 1
max_consecutive_no_progress = 3
max_consecutive_same_blocker = 3
role_timeout_minutes = 45
```

Failures are classified as:

- **Infrastructure:** unavailable Codex, adapter executable, network, disk, process, or credentials. The run becomes blocked; this is not a product defect.
- **Candidate:** build, runtime, test, replay, visual, or acceptance failure attributable to the candidate. The issue ledger records a gap for the next loop.
- **Protocol:** invalid structured output, protected-path change, candidate mismatch, missing cited file, invalid hash, or QA write attempt. The phase is rejected and retained for audit.

A role may receive one automatic retry for a transient process failure or repairable schema failure. A retry uses a new invocation and retains the failed attempt. Candidate failures are not retried invisibly; they close the loop as evidence.

Resume starts at the first incomplete phase after validating all preceding artifacts and hashes. It never repeats a completed Developer phase or creates duplicate commits.

## 15. Completion and Stop Policy

A product is complete only when the latest candidate:

1. Passes every required deterministic adapter check.
2. Has verified evidence for every required PRD claim.
3. Has no open blocker or major issue.
4. Passes a fresh, full release QA invocation and end-to-end gate.

The run stops when any of these conditions holds:

- Product completion is verified.
- Loop, elapsed-time, or configured token budget is exhausted.
- The same blocker remains for three consecutive loops.
- No measurable evidence progress occurs for three consecutive loops.
- Infrastructure or protocol failure cannot be repaired within the retry policy.
- External credentials, payment, licensing, publishing, or a product decision requires user action.
- The user requests cancellation.

Every terminal state writes `run-summary.md` with the best candidate, current candidate, remaining gaps, costs, failure category, and exact resume guidance.

## 16. Security and Authentication

The backend applies least privilege: `read-only` for Planner and QA, `workspace-write` for Developer, and no `danger-full-access` in the default configuration. Commands and environment variables included in receipts are redacted for secret-like values.

The harness does not ask users to paste access tokens into prompts or store them in `.hoh/`. Codex authentication remains owned by the installed Codex client. GitHub publication uses the user's Git credential manager or authenticated GitHub CLI when available.

The current workstation does not have GitHub CLI installed. Local implementation and commits do not depend on it. Before publishing, the workflow will either use existing Git credentials with `git push` or stop with instructions to install and authenticate `gh`; it will not embed a token.

## 17. Test Strategy

### 17.1 Unit tests

Unit tests cover TOML configuration, schema validation, phase transitions, atomic state writes, receipt hashes, issue derivation, progress calculation, skill selection, stopping rules, and redaction.

### 17.2 Integration tests

`FakeAgentBackend` and temporary Git repositories cover successful loops, Planner schema repair, Developer protected-path rejection, candidate commits, detached QA worktrees, evidence binding, candidate failure, infrastructure failure, repeated blockers, no-progress stopping, and crash-safe resume.

### 17.3 Codex contract tests

Recorded synthetic JSONL streams cover process success, nonzero exit, malformed events, timeout, missing final response, token accounting, and schema-constrained output. Tests assert exact sandbox selection for each role.

### 17.4 End-to-end tests

A real one-loop Codex smoke test is opt-in because it consumes model quota. It runs against a tiny fixture and proves that three genuine invocations produced a plan, production diff, frozen candidate, checks, and evidence.

Godot adapter contract tests use a fake executable by default. A real minimal Godot E2E test is separately opt-in and must report `blocked` until Godot is installed. CI must never convert a skipped prerequisite into a passing product check.

## 18. Acceptance Criteria

The rewrite is acceptable when:

1. `hoh init` initializes a separate Git product with explicit backend, model, reasoning effort, and adapter settings.
2. `hoh doctor` distinguishes healthy, blocked, and misconfigured prerequisites.
3. `hoh run` can complete a full three-role loop with structured outputs and candidate-bound evidence.
4. Planner and QA run read-only; Developer runs workspace-write; protected state remains host-owned.
5. A fixed candidate SHA and artifact hash are used by deterministic checks and QA.
6. Missing tools, timeouts, invalid schemas, missing evidence, and runtime errors cannot yield a false pass.
7. An interrupted run resumes without duplicating a completed role or commit.
8. Completion, budget, blocker, and no-progress stop conditions are covered by tests.
9. `hoh status --json` and `hoh report` expose the best candidate, current state, usage, and remaining gaps.
10. The normal automated test suite passes on Windows and does not require model quota or Godot.
11. The opt-in real Codex smoke test demonstrates genuine role invocations when credentials and quota are available.
12. Documentation accurately distinguishes paper-derived concepts from project-specific engineering decisions.

## 19. Migration and Delivery

The existing remote `main` head is retained locally as branch `legacy/voidknight` and tag `legacy-voidknight-2026-09-07`. Before replacing the remote default-branch contents, both refs will be pushed so the legacy project is recoverable from GitHub.

Implementation occurs on `codex/hoh-rewrite`. The old game, generated receipts, caches, and previous runner will be removed from the rewrite branch as an explicit commit, followed by the new package and tests. No unrelated remote repository is changed.

After verification, the rewrite branch will be pushed and the remote `main` will be updated without force-pushing. If branch protection or missing authentication prevents publication, local commits remain complete and the process stops with the exact manual action required.

Neon Relay begins only after this harness meets the acceptance criteria above. It will receive its own product design, implementation plan, and target repository decision.
