# Methodology

## Scope and source boundary

This project is an independent, evidence-grounded implementation inspired by
[*Harness-of-Harness: Multi-Day Autonomous Software Development with Continual
Improvement*](https://arxiv.org/abs/2609.01481). It does not claim to reproduce
the paper authors' unreleased HoH-lite or private runtime. The mapping below
states how this repository interprets the public method; the concrete schemas,
Git protocol, adapters, retry rules, and CLI are this project's engineering
choices.

## Paper concept to implementation mapping

| Concept | Methodological role | This implementation |
|---|---|---|
| Artifact state | The evolving product carries forward across development loops. | Production files live in a separate Git repository. Each Developer increment becomes a candidate commit on `hoh/run-<run-id>`; later loops start from retained repository state rather than a generated narrative. |
| Evidence state | Evaluation results carry forward alongside the artifact so future work is grounded in observed behavior. | Candidate-bound `checks.json`, `adapter-manifest.json`, normalized `evidence.json`, receipts, and the issue ledger are retained under `.hoh/runs/`. The next Planner receives normalized prior evidence and stable issue identities. |
| Planner | Select the next coherent improvement from the current product and evaluation state. | A fresh read-only Codex invocation receives the PRD, requirement registry, project summary, prior evidence, issue ledger, and selected skills. Its schema allows one to three priorities and forbids a completion verdict. |
| Developer | Change the artifact in pursuit of the bounded plan. | A fresh workspace-write invocation is the only agent allowed to edit production files. The host snapshots `.hoh` and `.git`, rejects protected-path changes, derives changed paths from Git, and commits the candidate itself. |
| QA Tester | Independently assess what the current artifact actually demonstrates. | A fresh read-only invocation inspects a detached worktree at one candidate SHA plus host-retained deterministic check records. A verified claim needs a cited execution record; missing proof is a gap or insufficient evidence, not success. |
| Warm start | Begin a new role/loop with accumulated public state instead of relying on an unbounded conversation. | Every role is a new `codex exec --ephemeral` process. Warmth comes from materialized PRD, requirements, candidate state, normalized evidence, issue history, project summary, and versioned skill content—not hidden chat memory. |
| Bounded objective | Keep each increment focused and make long-running work terminate under explicit limits. | Planner output has at most three related priorities. The host applies loop, elapsed-time, token, retry, repeated-blocker, and no-progress limits; agent-reported file creation alone is not progress. |
| Independent acceptance | Separate implementation from the decision that observable requirements are met. | Developer self-tests are informative only. The command or Godot adapter checks a frozen candidate, QA cites retained records, host normalization recomputes completion, and a candidate that appears complete must pass a distinct full-release check and QA invocation. |

## State carried between loops

The two state streams remain distinct:

- **Artifact state** is the product Git tree and its candidate history. A
  candidate commit contains production changes only. The direct child evidence
  commit contains only host-selected run records and retained artifacts.
- **Evidence state** is the normalized QA record, deterministic adapter record,
  issue-ledger transition history, invocation receipts, and stop decision. It
  describes what was checked about a specific artifact; it is not allowed to
  mutate or stand in for that artifact.

The next Planner sees both streams through bounded public context. It can
preserve verified behavior, prioritize open or regressed issues, and request
new acceptance evidence without inheriting a role's private conversation.

## Host and role authority

The agent roles propose or assess; the host enforces:

| Decision or mutation | Owner |
|---|---|
| Plan content | Planner, constrained by JSON Schema and priority limit |
| Production edits | Developer, constrained by workspace sandbox and protected paths |
| Candidate commit and detached QA worktree | Host |
| Deterministic commands and artifact collection | Host-selected adapter |
| Claim observations and gap recommendations | QA, constrained by JSON Schema |
| Candidate/hash/path binding and completion derivation | Host |
| Issue lifecycle, phase journal, receipts, retries, budgets, and stop status | Host |
| Merge, push, publication, purchases, credentials, and product decisions | Human operator |

This boundary prevents a role's prose from becoming a host fact merely because
it appeared in structured output.

## Engineering additions in this repository

The following mechanisms are repository-specific additions rather than claims
about the paper authors' private runtime:

1. Draft 2020-12 JSON Schemas for requirements, plans, Developer reports, and
   QA evidence, plus semantic checks for iteration and configured priority
   limits.
2. A two-commit loop protocol: production-only candidate commit followed by a
   host-selected evidence commit, with durable prepared-commit intent for crash
   windows.
3. SHA-256 descriptors for phase artifacts, candidate-tree binding, manifest
   binding for every cited execution record, and fail-closed replay during
   `status` and `report`.
4. A product lock, atomic state replacement, idempotent phase keys, and resume
   from the first incomplete phase without repeating a completed Developer or
   candidate commit.
5. A deterministic issue-ledger reducer with `open`, `closed`, and `regressed`
   transitions, evidence-application hashes, and exact replay validation.
6. Shell-free command and strict Godot adapters with explicit missing-tool,
   nonzero-exit, timeout, error-pattern, and missing-artifact semantics.
7. Per-attempt receipts that retain prompt, model, reasoning, sandbox, schema,
   event-stream, executable-version, usage, skill-version, and outcome
   identities while keeping Codex authentication outside project state.
8. Stable CLI exit codes, deterministic human/JSON reports, explicit stopping
   precedence, and candidate-only manual merge guidance.

## Completion interpretation

Ordinary QA output cannot declare a product complete by itself. The host first
requires passing deterministic checks, verified records for every required
claim, and no blocker or major gap on the candidate. It then runs a separate
full-release check and fresh QA gate against the same candidate. Only the
host-owned policy can emit `complete`, and the harness still stops at merge
guidance for a human operator.

This is an auditable engineering definition of completion for this tool. It is
not proof that the chosen PRD is exhaustive, that qualitative QA judgment is
infallible, or that the implementation matches an unreleased reference system.
