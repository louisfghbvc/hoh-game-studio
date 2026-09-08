# HoH Game Studio

HoH Game Studio is an evidence-grounded, **Harness-of-Harness (HoH)-inspired**
orchestrator for bounded software-development loops. It applies ideas described
in [*Harness-of-Harness: Multi-Day Autonomous Software Development with
Continual Improvement*](https://arxiv.org/abs/2609.01481), but it is not an
exact or complete reproduction. The paper authors have not published HoH-lite
or their full private runtime, so this project implements an independently
designed workflow around public concepts and explicit engineering choices.

The harness is a Python package; the product it changes is a separate Git
repository. Each loop starts fresh Planner, Developer, and QA Codex processes,
freezes the resulting Git candidate, retains deterministic check output, and
binds QA evidence to that candidate before the host decides whether to
continue.

## Architecture

One loop follows this host-owned path:

1. Validate configuration, Git cleanliness, tools, budgets, and adapter
   prerequisites.
2. Ask a fresh Planner for one to three priorities from the PRD, requirement
   registry, prior evidence, issue ledger, and a content-free project summary.
3. Run adapter baseline checks, then let a fresh Developer edit production
   files.
4. Reject protected-path changes and create a candidate commit on a dedicated
   `hoh/run-<run-id>` branch.
5. Check a detached worktree at the exact candidate commit and collect retained
   artifacts outside that worktree.
6. Ask a fresh QA process to assess the frozen candidate, normalize and hash
   its evidence, update the issue ledger, and evaluate the stopping policy.
7. If the ordinary result appears complete, run a separate full-release check
   and QA gate against the same candidate before reporting completion.

Agent output is advisory at the host boundary. The host owns phase state,
schemas, Git commits, hashes, receipts, issue transitions, budgets, and stop
decisions. See [Methodology](docs/methodology.md) for the paper-to-code mapping
and [Evidence format](docs/evidence-format.md) for the durable record contract.

## Prerequisites

- Python 3.10 or newer.
- Git and a clean target product repository with at least one commit.
- The Codex CLI available as `codex` (or configured through `codex_bin`), with
  authentication, model access, and quota suitable for real runs.
- Every executable named by the selected product adapter.
- Godot 4.x only when using the Godot adapter; it is not bundled.

`hoh doctor` checks the configured product before a run. A missing executable,
invalid requirements registry, dirty worktree, existing run lock, unwritable
state directory, or unavailable disk space is blocking rather than a pass.

## Installation

From this repository checkout:

```console
python -m pip install -e .
```

Install the offline test dependency when developing the harness:

```console
python -m pip install -e ".[test]"
```

The package installs the `hoh` console command. This repository intentionally
does not claim an open-source license without a separately selected `LICENSE`.

## Initialize a product

Initialize HoH state in an existing Git product repository. Choose a model and
reasoning effort that your installed Codex client supports:

```console
hoh init --project ./my-product --adapter command --model gpt-5.3-codex --reasoning-effort high
```

`init` creates `.hoh/config.toml`, `.hoh/prd.md`,
`.hoh/requirements.json`, `.hoh/issue-ledger.json`, and `.hoh/runs/` without
overwriting existing files. Fill in the PRD and add at least one required,
observable claim before running. For example:

```json
{
  "schema_version": 1,
  "claims": [
    {
      "id": "cli-greets-user",
      "description": "The CLI prints a greeting for the supplied name",
      "required": true
    }
  ]
}
```

For the command adapter, put shell-free argument vectors under the generated
`[adapter_options]` table:

```toml
[adapter_options]
checks = [["python", "-m", "pytest", "-q"]]
required_artifacts = []
artifact_globs = ["test-results/**/*.json", "screenshots/**/*.png"]
error_patterns = ["Traceback", "ERROR:"]
entrypoints = ["src/app.py"]
timeout_seconds = 120
```

Commands are executed directly, never through a shell. Each nonzero exit,
timeout, missing executable, missing required artifact, or configured error
pattern prevents that check from passing.

## Check and run

Check the same prerequisites used by `run`:

```console
hoh doctor --project ./my-product
```

Start a bounded run, optionally lowering the configured loop limit:

```console
hoh run --project ./my-product --max-loops 3
```

The harness creates and checks out `hoh/run-<run-id>` in the product
repository. It creates candidate and evidence commits there; it does not merge,
push, reset, publish, or change the product's default branch.

Inspect the newest durable run in human or machine-readable form:

```console
hoh status --project ./my-product
hoh status --project ./my-product --json
```

Resume the newest resumable run, optionally binding the command to its exact
ID:

```console
hoh resume --project ./my-product
hoh resume --project ./my-product --run-id 20260908T010203Z-0123456789ab
```

Render and atomically refresh its deterministic Markdown report:

```console
hoh report --project ./my-product
```

List the packaged skill IDs, versions, role/adapter applicability, and hashes:

```console
hoh skills list
```

Stable exit codes are `0` for success or verified completion, `2` for usage or
configuration errors, `3` for blocked prerequisites or infrastructure, `4`
for a bounded incomplete candidate run, `5` for protocol/state rejection, and
`130` for cancellation.

## Role sandboxes

| Role | Codex sandbox | Workspace | Authority |
|---|---|---|---|
| Planner | `read-only` | Product repository | Plans only; cannot edit product or host state |
| Developer | `workspace-write` | Product repository | Sole production writer; `.hoh` and `.git` remain protected |
| QA | `read-only` | Detached worktree at the candidate SHA | Assesses retained checks and evidence; cannot repair the candidate |

The host snapshots read-only workspaces and protected paths around invocations.
A mutation at either boundary is a retained protocol violation.

## Evidence guarantees

- Candidate records bind the loop to a resolvable Git commit, its parent, the
  selected changed paths, and a SHA-256 derived from the candidate tree ID.
- Deterministic checks and adapter manifests carry the same candidate SHA and
  artifact-tree hash. Collected evidence files are retained with SHA-256
  hashes.
- A verified QA claim must cite a retained manifest path whose on-disk bytes,
  QA hash, and manifest hash all agree. Traversal and cross-candidate evidence
  are rejected.
- Host normalization recomputes completion from required claim IDs, check
  status, and blocker/major gaps; it does not trust QA's global completion
  boolean.
- Per-attempt receipts retain the role, sandbox, model, reasoning effort,
  prompt hash, schema, event path, executable version, outcome, token usage,
  and selected skill hashes. Failed and repair attempts remain visible.
- The issue ledger is reconstructed from durable normalized evidence during
  inspection. Aggregate status cannot silently replace or contradict the
  candidate-bound records.
- A completed loop stores production changes in a candidate commit and
  host-selected evidence in its direct child evidence commit. Completion
  guidance names the candidate commit, not the evidence commit.

These checks make the records auditable; they do not prove that an acceptance
claim is well chosen or that an agent's qualitative judgment is correct.

## Stopping defaults

All limits are explicit in `.hoh/config.toml`:

| Setting | Default | Meaning |
|---|---:|---|
| `max_loops` | 12 | Maximum completed loops |
| `max_priorities_per_loop` | 3 | Maximum related Planner priorities |
| `max_role_retries` | 1 | One fresh repair attempt after an eligible role failure |
| `max_consecutive_no_progress` | 3 | Stop after three evidence-no-progress loops |
| `max_consecutive_same_blocker` | 3 | Stop after the same blocker persists three loops |
| `role_timeout_minutes` | 45 | Timeout for each role attempt |
| `max_total_tokens` | 5,000,000 | Host-accounted run token ceiling |
| `max_elapsed_minutes` | 480 | Host-accounted elapsed-time ceiling |

The host evaluates verified completion first, then cancellation,
unrecoverable infrastructure/protocol failure, repeated blockers, repeated lack
of measurable evidence progress, token budget, elapsed-time budget, and loop
budget. External credentials, payment, licensing, publishing, or unresolved
product decisions still require a person.

## Godot configuration

Initialize with `--adapter godot`, then configure the generated table. The
adapter always performs a strict headless editor import. Additional test and
replay entries are argument lists appended to the configured Godot command;
`{project}` is replaced with the resolved Godot project directory.

```toml
adapter = "godot"

[adapter_options]
command = ["godot"]
project_subdir = "."
test_commands = [["--headless", "--path", "{project}", "--script", "res://tests/run.gd"]]
replay_commands = [["--headless", "--path", "{project}", "--script", "res://tests/replay.gd"]]
required_evidence_globs = ["evidence/**/*.jsonl", "evidence/**/*.png"]
timeout_seconds = 120
```

Missing `project.godot`, missing Godot, a timeout, nonzero exit, runtime error
patterns (`SCRIPT ERROR`, `Parse Error`, or `ERROR:`), or an unmatched required
evidence glob is a failure or blocker. The included `examples/minimal-godot`
fixture demonstrates clean headless boot only; it does not fabricate gameplay
evidence.

## Opt-in real Codex smoke test

The normal offline suite collects this test but skips it. **Do not opt in
casually:** it invokes the real Codex CLI, consumes account quota, may make a
repair attempt, requires working authentication/model access, and can take up
to the configured role timeouts. It uses a temporary text-only Git product,
one loop, the public service factory, and a real `CodexExecBackend`; it does not
run Godot.

PowerShell:

```powershell
$env:HOH_REAL_CODEX_E2E = "1"
python -m pytest tests/e2e/test_real_codex.py -q -rs
Remove-Item Env:HOH_REAL_CODEX_E2E
```

POSIX shell:

```sh
HOH_REAL_CODEX_E2E=1 python -m pytest tests/e2e/test_real_codex.py -q -rs
```

CI never sets `HOH_REAL_CODEX_E2E` and never supplies Codex credentials.

## Legacy project

The pre-rewrite VoidKnight project is preserved on the
[`legacy/voidknight`](https://github.com/louisfghbvc/hoh-game-studio/tree/legacy/voidknight)
branch and the
[`legacy-voidknight-2026-09-07`](https://github.com/louisfghbvc/hoh-game-studio/tree/legacy-voidknight-2026-09-07)
tag. The rewrite keeps normal Git history and does not require a force-push.

## Limitations

- This is a first milestone inspired by public HoH concepts, not the authors'
  unreleased implementation or a claim of paper-level parity.
- The only production agent backend is a subprocess wrapper around
  `codex exec`; there is no hosted service, dashboard, distributed scheduler,
  or SDK backend yet.
- Command and Godot adapters can retain objective records, but product-specific
  acceptance design, deterministic fixtures, and evidence quality remain the
  product owner's responsibility.
- Godot is optional and not installed or exercised as a real engine in normal
  CI. Only its adapter contract is tested offline with a fixture executable.
- Runs recommend an exact resume or merge command but never perform merge,
  push, release, purchase, licensing, or account changes.
- Codex authentication stays with the installed client. Secrets must not be
  placed in prompts or `.hoh` state.
