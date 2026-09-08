# Evidence format

This reference describes the public structured artifacts produced for each HoH
loop. Paths below are relative to
`.hoh/runs/<run-id>/loops/loop-<four digits>/` unless stated otherwise.

Planner, Developer, and raw QA responses use Draft 2020-12 JSON Schemas with
`additionalProperties: false`. The host then adds explicitly documented
normalization fields to QA evidence. JSON examples use full-length lowercase
hex strings; in a real run, every candidate and file hash must also match the
Git object or retained bytes it identifies.

## Plan: `plan.json`

| Field | Type | Meaning |
|---|---|---|
| `iteration` | positive integer | Loop number; must equal the host's active loop. |
| `objective` | nonempty string | Observable outcome for this increment. |
| `priorities` | array of 1-3 priority objects | Ordered, related work items. The configured host limit may be lower than three. |
| `preservation_constraints` | array of nonempty strings | Previously verified behavior that must remain intact. |
| `acceptance_gate` | nonempty array of nonempty strings | Claim IDs or explicit checks needed to accept the increment. |

Each `priorities[]` object has exactly these fields:

| Field | Type | Meaning |
|---|---|---|
| `id` | nonempty string | Stable priority identifier within the plan. |
| `title` | nonempty string | Short priority label. |
| `implementation_target` | nonempty string | Product area or behavior to change. |
| `acceptance_claims` | nonempty array of nonempty strings | Claim IDs the change is intended to support. |
| `source_gap_ids` | array of nonempty strings | Prior gap/issue IDs motivating the priority; empty on the first loop when appropriate. |

Valid example:

```json
{
  "iteration": 1,
  "objective": "Make the text product report DONE",
  "priorities": [
    {
      "id": "priority-done",
      "title": "Update the product state",
      "implementation_target": "product.txt",
      "acceptance_claims": ["product-text-done"],
      "source_gap_ids": []
    }
  ],
  "preservation_constraints": ["Keep product.txt UTF-8 text"],
  "acceptance_gate": ["product-text-done"]
}
```

## Developer report: `developer-report.json`

The report is informative; Git and adapter records, not these claims, determine
what changed and what passed.

| Field | Type | Meaning |
|---|---|---|
| `summary` | nonempty string | Developer's concise description; also supplies the host-controlled candidate commit subject suffix. |
| `completed_priority_ids` | array of nonempty strings | Priority IDs the Developer reports completing. |
| `self_tests` | array of nonempty strings | Checks the Developer reports running. These are not acceptance evidence by themselves. |
| `known_gaps` | array of nonempty strings | Limitations the Developer reports leaving. |

Valid example:

```json
{
  "summary": "write the required DONE state",
  "completed_priority_ids": ["priority-done"],
  "self_tests": ["read product.txt and compare exact text"],
  "known_gaps": []
}
```

## Candidate: `candidate.json`

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer, currently `1` | Candidate record format. |
| `run_id` | nonempty string | Owning run. |
| `loop_index` | positive integer | Owning loop. |
| `candidate_sha` | Git commit SHA | Production-only candidate commit created by the host. |
| `artifact_tree_sha256` | 64 lowercase hex characters | SHA-256 of the candidate Git tree object's hexadecimal ID encoded as ASCII. |
| `changed_paths` | array of strings | Host-derived production paths selected for the candidate commit; `.hoh` is excluded. |

The adjacent `candidate-commit-intent.json` additionally binds the expected
parent, prepared commit/tree IDs, selected paths, and selected file hashes so a
crash after creating the Git object cannot silently change the intended
candidate.

Valid example:

```json
{
  "schema_version": 1,
  "run_id": "20260908T010203Z-0123456789ab",
  "loop_index": 1,
  "candidate_sha": "1111111111111111111111111111111111111111",
  "artifact_tree_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "changed_paths": ["product.txt"]
}
```

## Deterministic checks

### Shared check bundle

`baseline-checks.json` is a shared check bundle. Ordinary `checks.json` carries
the same bundle fields at top level and adds candidate bindings.

| Field | Type | Meaning |
|---|---|---|
| `adapter` | string | Adapter ID, currently `command` or `godot`. |
| `status` | `pass`, `fail`, or `blocked` | Derived bundle result. `blocked` takes precedence over `fail`, which takes precedence over `pass`. |
| `results` | array of check-result objects | Individual deterministic outcomes in adapter order. |
| `artifact_paths` | array of strings | Retained public check-record paths produced by the bundle. |

Each `results[]` check result has:

| Field | Type | Meaning |
|---|---|---|
| `check_id` | string | Stable adapter-local check identity. |
| `status` | `pass`, `fail`, or `blocked` | Individual outcome. |
| `summary` | string | Deterministic host explanation. |
| `artifact_paths` | array of strings | Output, error, metadata, or other records associated with this check. |

Ordinary candidate `checks.json` adds:

| Field | Type | Meaning |
|---|---|---|
| `check_id` | nonempty string | Run/loop-scoped deterministic check identity. |
| `candidate_sha` | Git commit SHA | Frozen candidate that was checked. |
| `artifact_tree_sha256` | 64-character hash | Candidate tree binding copied from `candidate.json`. |

Valid ordinary-check example:

```json
{
  "adapter": "command",
  "status": "pass",
  "results": [
    {
      "check_id": "command:0001",
      "status": "pass",
      "summary": "Command completed successfully",
      "artifact_paths": [
        "adapter/checks/command-0001.stdout.txt",
        "adapter/checks/command-0001.stderr.txt",
        "adapter/checks/command-0001.json"
      ]
    }
  ],
  "artifact_paths": [
    "adapter/checks/command-0001.stdout.txt",
    "adapter/checks/command-0001.stderr.txt",
    "adapter/checks/command-0001.json"
  ],
  "check_id": "20260908T010203Z-0123456789ab:loop-0001:candidate-check",
  "candidate_sha": "1111111111111111111111111111111111111111",
  "artifact_tree_sha256": "2222222222222222222222222222222222222222222222222222222222222222"
}
```

### Full-release checks

`release-checks.json` wraps a fresh bundle with these fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer, currently `1` | Release-check record format. |
| `check_id` | string | Run/loop-scoped full-release check identity. |
| `scope` | `full_release` | Prevents substitution of an ordinary candidate check. |
| `candidate_sha` | Git commit SHA | Frozen candidate that was rechecked. |
| `artifact_tree_sha256` | 64-character hash | Candidate tree binding. |
| `bundle` | shared check-bundle object | Fresh adapter results with every shared field documented above. |

A release result is not interchangeable with the ordinary candidate check.

The QA phase journal also retains a `release_gate` object with every field
below when the separate gate runs:

| Field | Type | Meaning |
|---|---|---|
| `invocation_id` | string | Distinct full-release QA attempt identity. |
| `candidate_sha` | Git commit SHA | Candidate assessed by the gate. |
| `artifact_tree_sha256` | 64-character hash | Candidate tree binding. |
| `scope` | `full_release` | Prevents an ordinary assessment from being substituted. |
| `qa_status` | `pass` or `fail` | Derived from normalized release QA status and completion. |
| `end_to_end_passed` | boolean | Whether the release adapter bundle passed. |
| `deterministic_checks_passed` | boolean | Same host-derived adapter verdict, retained explicitly for policy validation. |
| `check_id` | string | Full-release deterministic check identity. |
| `deterministic_checks_candidate_sha` | Git commit SHA | Explicit candidate binding for the checks. |
| `checks` | descriptor object | Project-relative `path` and file `sha256` for `release-checks.json`. |
| `manifest` | descriptor object | Project-relative `path` and file `sha256` for `release-adapter-manifest.json`. |
| `evidence` | descriptor object | Project-relative `path` and file `sha256` for `release-evidence.json`. |

For command checks, each `command-NNNN.json` record has `command` (the exact
argument array), `return_code` (integer or null), ISO-8601 `started_at` and
`ended_at`, and boolean `timed_out`; adjacent `.stdout.txt` and `.stderr.txt`
files retain process output. These records are data, not commands to replay.

### Adapter manifest

`adapter-manifest.json` binds collected bytes to the check:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer, currently `1` | Manifest format. |
| `adapter` | string | Adapter ID. |
| `status` | `pass`, `fail`, or `blocked` | Adapter bundle status. |
| `candidate_sha` | Git commit SHA | Candidate binding. |
| `artifact_tree_sha256` | 64-character hash | Candidate tree binding. |
| `deterministic_check_id` | string | Must equal the corresponding `checks.json` `check_id`. |
| `artifacts` | object from relative path to SHA-256 | Every file QA may cite. Paths are contained under the loop directory. |
| `diagnostics` | array | Host infrastructure/protocol diagnostics kept separate from product gaps. |

`release-adapter-manifest.json` has the same fields plus `scope` equal to
`full_release`.

Valid manifest example (the hash is SHA-256 of the UTF-8 bytes `DONE\n`):

```json
{
  "schema_version": 1,
  "adapter": "command",
  "status": "pass",
  "candidate_sha": "1111111111111111111111111111111111111111",
  "artifact_tree_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "deterministic_check_id": "20260908T010203Z-0123456789ab:loop-0001:candidate-check",
  "artifacts": {
    "adapter/artifacts/product.txt": "8221ac66be71558c921fb44cfb66f7997699aea754d917763882d6d9eddc836e"
  },
  "diagnostics": []
}
```

## QA evidence: `evidence.json`

### Raw QA fields

The QA response schema requires exactly these top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `iteration` | positive integer | Loop number; must equal the active loop. |
| `candidate_sha` | 40-64 lowercase hex characters | Candidate assessed by QA. |
| `artifact_tree_sha256` | 64 lowercase hex characters | Candidate tree binding. |
| `qa_status` | `pass`, `fail`, or `insufficient_evidence` | QA's verdict for this assessment. |
| `product_complete` | boolean | QA's advisory global verdict. The host does not trust it directly. |
| `verified_records` | array of verified-record objects | Claims supported by retained execution records. At least one is required when QA sets `product_complete` to true. |
| `gap_records` | array of gap-record objects | Unsupported or failed claims. |
| `planner_handoff` | object | Bounded context for the next Planner. |

Each `verified_records[]` object has:

| Field | Type | Meaning |
|---|---|---|
| `claim_id` | nonempty string | ID from `.hoh/requirements.json`. A claim may appear only once across verified and gap records in a loop. |
| `claim` | nonempty string | Human-readable claim assessed. |
| `observations` | nonempty array of nonempty strings | QA observations. |
| `execution_records` | nonempty array of execution-record objects | Retained proof for the claim. |
| `preservation_requirement` | nonempty string | Behavior a later loop must preserve. |

Each `execution_records[]` object has:

| Field | Type | Meaning |
|---|---|---|
| `type` | nonempty string | Evidence kind, such as `text-check`, `test-log`, `telemetry`, or `screenshot`. |
| `path` | nonempty relative string | Exact key in the adapter manifest. Absolute paths and traversal are rejected. |
| `sha256` | 64 lowercase hex characters | Must match both the manifest value and actual retained bytes. |
| `observation` | nonempty string | What this particular retained record demonstrates. |

Each `gap_records[]` object has:

| Field | Type | Meaning |
|---|---|---|
| `claim_id` | nonempty string | Known requirement claim ID. |
| `severity` | `blocker`, `major`, or `minor` | Product impact classification. Blocker/major gaps prevent host completion. |
| `impact` | nonempty string | User or product consequence. |
| `observations` | nonempty array of nonempty strings | What QA observed or could not demonstrate. |
| `recommended_update` | nonempty string | Suggested next product change. |
| `validation_requirement` | nonempty string | Proof needed to close the gap. |

`planner_handoff` contains:

| Field | Type | Meaning |
|---|---|---|
| `preservation_constraints` | array of nonempty strings | Verified behavior to retain. |
| `update_targets` | array of nonempty strings | Next product areas or claim IDs to address. |
| `validation_requirements` | array of nonempty strings | Required future checks/evidence. |

### Host-added normalized fields

Before writing `evidence.json`, the host sorts records, verifies every binding,
and replaces `product_complete` with its own derivation. It then adds:

| Field | Type | Meaning |
|---|---|---|
| `host_metadata` | object | Inputs and outcome of host completion normalization. |
| `diagnostics` | array | Deterministically sorted manifest diagnostics, distinct from product gaps. |

`host_metadata` has exactly these fields:

| Field | Type | Meaning |
|---|---|---|
| `qa_product_complete` | boolean | Original QA-supplied `product_complete` value retained for audit. |
| `deterministic_checks_passed` | boolean | True only when the adapter manifest status is `pass`. |
| `required_claim_ids` | sorted array of strings | Required IDs from the host registry. |
| `verified_required_claim_ids` | sorted array of strings | Required IDs present in verified records on this candidate. |

A structurally valid normalized example, bound to the manifest above:

```json
{
  "iteration": 1,
  "candidate_sha": "1111111111111111111111111111111111111111",
  "artifact_tree_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "qa_status": "pass",
  "product_complete": true,
  "verified_records": [
    {
      "claim_id": "product-text-done",
      "claim": "product.txt contains exactly DONE followed by a newline",
      "observations": ["The retained text is exactly DONE followed by a newline."],
      "execution_records": [
        {
          "type": "text-check",
          "path": "adapter/artifacts/product.txt",
          "sha256": "8221ac66be71558c921fb44cfb66f7997699aea754d917763882d6d9eddc836e",
          "observation": "The retained UTF-8 bytes decode to DONE followed by one newline."
        }
      ],
      "preservation_requirement": "Keep the exact DONE output."
    }
  ],
  "gap_records": [],
  "planner_handoff": {
    "preservation_constraints": ["Keep the exact DONE output."],
    "update_targets": [],
    "validation_requirements": ["Re-run the exact text check after changes."]
  },
  "host_metadata": {
    "qa_product_complete": true,
    "deterministic_checks_passed": true,
    "required_claim_ids": ["product-text-done"],
    "verified_required_claim_ids": ["product-text-done"]
  },
  "diagnostics": []
}
```

This document can support entry into the separate full-release gate. It does
not by itself prove the run complete.

## Receipts

### Per-attempt receipt: `receipts/<role>[-<kind>]-attempt-NN.json`

| Field | Type | Meaning |
|---|---|---|
| `invocation_id` | string | Run/loop/role/kind/attempt identity. |
| `role` | `planner`, `developer`, or `qa` | Invoked role. |
| `kind` | string | `ordinary` or `full-release`. |
| `attempt` | positive integer | Attempt number; the current policy permits at most 2. |
| `outcome` | string | `success`, `schema_invalid`, `error`, or `policy_violation`. |
| `model` | string | Explicit configured model ID. |
| `reasoning_effort` | string | Explicit configured reasoning effort. |
| `sandbox` | `read-only` or `workspace-write` | Effective Codex sandbox. |
| `timeout_seconds` | positive integer | Per-attempt subprocess timeout. |
| `schema_path` | string | Packaged response schema filename. |
| `prompt_sha256` | 64-character hash | SHA-256 of exact prompt UTF-8 bytes. |
| `events_path` | string | Retained JSONL event stream. Boundary violations use a `host-staging/` audit path. |
| `skills` | array of skill receipts | Ordered skills injected into the prompt. |
| `usage` | usage object | Token counters returned by the backend, or zeros when no result exists. |
| `return_code` | integer or null | Backend process return code, null when no result exists. |
| `executable_version` | string | Captured Codex executable version, or `unknown`. |
| `error` | object, optional | Present for non-success outcomes; contains `type` and `message` strings. |

Each `skills[]` object has `skill_id`, `version`, and content `sha256`. Each
`usage` object has `input_tokens`, `cached_input_tokens`, `output_tokens`, and
`reasoning_output_tokens`, all nonnegative integers.

Valid successful-attempt example:

```json
{
  "invocation_id": "20260908T010203Z-0123456789ab:loop-0001:planner:ordinary:attempt-01",
  "role": "planner",
  "kind": "ordinary",
  "attempt": 1,
  "outcome": "success",
  "model": "gpt-5.3-codex",
  "reasoning_effort": "high",
  "sandbox": "read-only",
  "timeout_seconds": 2700,
  "schema_path": "plan.schema.json",
  "prompt_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
  "events_path": ".hoh/runs/20260908T010203Z-0123456789ab/loops/loop-0001/planner-events-attempt-01.jsonl",
  "skills": [
    {
      "skill_id": "core.bounded-planning",
      "version": "1.0.0",
      "sha256": "4444444444444444444444444444444444444444444444444444444444444444"
    }
  ],
  "usage": {
    "input_tokens": 100,
    "cached_input_tokens": 20,
    "output_tokens": 30,
    "reasoning_output_tokens": 10
  },
  "return_code": 0,
  "executable_version": "codex-cli 0.104.0"
}
```

### Loop receipt: `receipt.json`

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer, currently `1` | Loop receipt format. |
| `run_id` | string | Owning run. |
| `loop_index` | positive integer | Owning loop. |
| `candidate_sha` | Git commit SHA | Candidate assessed in the loop. |
| `attempts` | array of per-attempt receipts | Every retained ordinary, repair, and full-release attempt, sorted by role/kind/attempt. |

Valid ordinary-loop example:

```json
{
  "schema_version": 1,
  "run_id": "20260908T010203Z-0123456789ab",
  "loop_index": 1,
  "candidate_sha": "1111111111111111111111111111111111111111",
  "attempts": [
    {
      "invocation_id": "20260908T010203Z-0123456789ab:loop-0001:planner:ordinary:attempt-01",
      "role": "planner",
      "kind": "ordinary",
      "attempt": 1,
      "outcome": "success",
      "model": "gpt-5.3-codex",
      "reasoning_effort": "high",
      "sandbox": "read-only",
      "timeout_seconds": 2700,
      "schema_path": "plan.schema.json",
      "prompt_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
      "events_path": ".hoh/runs/20260908T010203Z-0123456789ab/loops/loop-0001/planner-events-attempt-01.jsonl",
      "skills": [
        {
          "skill_id": "core.bounded-planning",
          "version": "1.0.0",
          "sha256": "4444444444444444444444444444444444444444444444444444444444444444"
        }
      ],
      "usage": {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "reasoning_output_tokens": 10
      },
      "return_code": 0,
      "executable_version": "codex-cli 0.104.0"
    },
    {
      "invocation_id": "20260908T010203Z-0123456789ab:loop-0001:developer:ordinary:attempt-01",
      "role": "developer",
      "kind": "ordinary",
      "attempt": 1,
      "outcome": "success",
      "model": "gpt-5.3-codex",
      "reasoning_effort": "high",
      "sandbox": "workspace-write",
      "timeout_seconds": 2700,
      "schema_path": "developer-report.schema.json",
      "prompt_sha256": "6666666666666666666666666666666666666666666666666666666666666666",
      "events_path": ".hoh/runs/20260908T010203Z-0123456789ab/loops/loop-0001/developer-events-attempt-01.jsonl",
      "skills": [],
      "usage": {
        "input_tokens": 200,
        "cached_input_tokens": 40,
        "output_tokens": 50,
        "reasoning_output_tokens": 20
      },
      "return_code": 0,
      "executable_version": "codex-cli 0.104.0"
    },
    {
      "invocation_id": "20260908T010203Z-0123456789ab:loop-0001:qa:ordinary:attempt-01",
      "role": "qa",
      "kind": "ordinary",
      "attempt": 1,
      "outcome": "success",
      "model": "gpt-5.3-codex",
      "reasoning_effort": "high",
      "sandbox": "read-only",
      "timeout_seconds": 2700,
      "schema_path": "evidence.schema.json",
      "prompt_sha256": "7777777777777777777777777777777777777777777777777777777777777777",
      "events_path": ".hoh/runs/20260908T010203Z-0123456789ab/loops/loop-0001/qa-events-attempt-01.jsonl",
      "skills": [
        {
          "skill_id": "core.evidence-grounded-qa",
          "version": "1.0.0",
          "sha256": "8888888888888888888888888888888888888888888888888888888888888888"
        }
      ],
      "usage": {
        "input_tokens": 150,
        "cached_input_tokens": 30,
        "output_tokens": 40,
        "reasoning_output_tokens": 10
      },
      "return_code": 0,
      "executable_version": "codex-cli 0.104.0"
    }
  ]
}
```

A successfully closed production loop normally contains at least Planner,
Developer, and QA attempt objects; completion adds a distinct full-release QA
attempt.

## Issue ledger: `.hoh/issue-ledger.json`

The initialized on-disk document is schema version 1 with `issues: []`. After
the first native evidence application the persisted document is schema version
2 with every field below.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | `1` for initialized/legacy state, `2` for native correlated applications. |
| `application_correlation_start_loop` | positive integer, v2 | First loop from which issue history is authenticated by application identity. |
| `issues` | array of issue objects | Claim-keyed issues sorted by `claim_id`. |
| `applications` | array of application objects | Strictly increasing evidence applications. |
| `summary` | object | Host-derived `total`, `open`, `closed`, and `regressed` counts. Supplied counts are ignored on load. |

Each `issues[]` object has:

| Field | Type | Meaning |
|---|---|---|
| `claim_id` | nonempty string | Stable issue identity. |
| `status` | `open`, `closed`, or `regressed` | Current state; valid transitions are open-to-closed, closed-to-regressed, and regressed-to-closed, with same-state observations allowed. |
| `severity` | `blocker`, `major`, or `minor` | Most recently applied gap severity. |
| `impact` | nonempty string | Most recently applied product/user impact. |
| `recommended_update` | nonempty string | Most recently applied repair recommendation. |
| `validation_requirement` | nonempty string | Most recently applied closure proof. |
| `history` | nonempty array of history events | Ordered lifecycle provenance. |

Each `history[]` event has exactly `loop`, `candidate`, `evidence_path`,
`observation`, and `status`. `evidence_path` is null for `open`/`regressed`
gap events and a retained execution-record path for `closed` events.

Each `applications[]` object has `loop`, `candidate`, and `evidence_sha256`.
The last value hashes canonical normalized evidence and prevents a different
document from being silently reapplied under the same loop identity.

Valid open-issue example:

```json
{
  "schema_version": 2,
  "application_correlation_start_loop": 1,
  "issues": [
    {
      "claim_id": "product-text-done",
      "status": "open",
      "history": [
        {
          "loop": 1,
          "candidate": "1111111111111111111111111111111111111111",
          "evidence_path": null,
          "observation": "The deterministic text check failed.",
          "status": "open"
        }
      ],
      "severity": "major",
      "impact": "The product does not report the required state.",
      "recommended_update": "Write DONE followed by one newline.",
      "validation_requirement": "Retain a passing exact-text check."
    }
  ],
  "applications": [
    {
      "loop": 1,
      "candidate": "1111111111111111111111111111111111111111",
      "evidence_sha256": "5555555555555555555555555555555555555555555555555555555555555555"
    }
  ],
  "summary": {
    "total": 1,
    "open": 1,
    "closed": 0,
    "regressed": 0
  }
}
```

## Status and run summary

`hoh status --json` and the structured input to `run-summary.md` are derived
from validated phase artifacts, receipts, evidence, Git identities, and issue
replay. The base status has every field below:

| Field | Type | Meaning |
|---|---|---|
| `run_id` | string or null | Durable run identity. |
| `terminal_status` | string or null | Authoritatively reconstructed as `running`, `resumable`, `complete`, `blocked`, `budget_exhausted`, or `cancelled`; null only when no decision record is supplied to the base reporter. |
| `reason` | string or null | Host stop/continue reason. |
| `start_sha` | string or null | Starting product commit. |
| `current_candidate` | string or null | Latest phase-bound candidate commit. |
| `best_candidate` | string or null | Completion-bound best candidate; authoritative inspection does not trust an arbitrary aggregate. |
| `completed_loops` | nonnegative integer | Number of validated closed loops. |
| `role_attempts` | object from role to count | Count derived from individual receipts. |
| `usage_by_role` | object from role to usage summary | Per-role token totals derived from receipts. |
| `total_tokens` | nonnegative integer | Sum of input, output, and reasoning-output tokens; cached input is reported separately, not added again. |
| `elapsed_seconds` | nonnegative integer | Host-accounted elapsed run time. |
| `verified_claim_ids` | sorted array of strings | Claim IDs observed in normalized loop evidence. |
| `remaining_gaps` | array of issue objects | Open or regressed ledger entries. |
| `issue_summary` | object | Derived `total`, `open`, `closed`, and `regressed` counts. |
| `failures` | array | Infrastructure/protocol diagnostics and/or terminal failure reason records. |
| `failure_category` | `infrastructure`, `protocol`, or null | Terminal classified failure when present. |
| `skills` | array of skill receipts | Unique `skill_id`, `version`, and `sha256` identities observed in receipts. |
| `guidance` | string | Exact safe `hoh resume --run-id ...`, candidate `git merge ...`, or manual-action text. |

`resumable` denotes a validated, repairable failure that interrupted the run.
Authoritative inspection reconstructs it only when the durable aggregate and
external structured failure record agree and the failure has
`"repairable": true`; for a safe run ID, the report guidance is
`hoh resume --run-id <run-id>`. It is distinct from `running` (an in-progress
run without a terminal closure), `cancelled` (a user-cancelled, non-resumable
outcome), `blocked` (a non-resumable failure or bounded policy stop described
below), `complete` (the release gate passed), and `budget_exhausted` (a
resource/loop-budget closure).

`blocked` has two classes of source. It records either a validated
unrecoverable `infrastructure` or `protocol` failure, or a bounded policy stop
after a configured streak threshold is reached. With the defaults, the policy
stops after three consecutive loops in which at least one same blocker
persists, or three consecutive loops with no measurable evidence progress.
`blocked` does not mean `budget_exhausted` (a token, elapsed-time, or loop
ceiling); it is not a user cancellation (`cancelled`) and not a repairable
failure state (`resumable`).

Each `usage_by_role.<role>` object has `input_tokens`,
`cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, and
`total_tokens`. Failure entries are either diagnostics with at least
`category` plus their diagnostic fields (commonly `code` and `message`) or a
derived `{category, reason}` terminal record.

The CLI adds two compatibility aliases to JSON and returned run results:
`status` equals `terminal_status`, and `loops_completed` equals
`completed_loops`.

Valid CLI JSON example:

```json
{
  "run_id": "20260908T010203Z-0123456789ab",
  "terminal_status": "budget_exhausted",
  "reason": "loop budget exhausted",
  "start_sha": "0000000000000000000000000000000000000000",
  "current_candidate": "1111111111111111111111111111111111111111",
  "best_candidate": null,
  "completed_loops": 1,
  "role_attempts": {
    "developer": 1,
    "planner": 1,
    "qa": 1
  },
  "usage_by_role": {
    "developer": {
      "input_tokens": 200,
      "cached_input_tokens": 40,
      "output_tokens": 50,
      "reasoning_output_tokens": 20,
      "total_tokens": 270
    },
    "planner": {
      "input_tokens": 100,
      "cached_input_tokens": 20,
      "output_tokens": 30,
      "reasoning_output_tokens": 10,
      "total_tokens": 140
    },
    "qa": {
      "input_tokens": 150,
      "cached_input_tokens": 30,
      "output_tokens": 40,
      "reasoning_output_tokens": 10,
      "total_tokens": 200
    }
  },
  "total_tokens": 610,
  "elapsed_seconds": 42,
  "verified_claim_ids": [],
  "remaining_gaps": [
    {
      "claim_id": "product-text-done",
      "status": "open",
      "severity": "major",
      "impact": "The product does not report the required state.",
      "recommended_update": "Write DONE followed by one newline.",
      "validation_requirement": "Retain a passing exact-text check.",
      "history": [
        {
          "loop": 1,
          "candidate": "1111111111111111111111111111111111111111",
          "evidence_path": null,
          "observation": "The deterministic text check failed.",
          "status": "open"
        }
      ]
    }
  ],
  "issue_summary": {
    "total": 1,
    "open": 1,
    "closed": 0,
    "regressed": 0
  },
  "failures": [],
  "failure_category": null,
  "skills": [
    {
      "skill_id": "core.bounded-planning",
      "version": "1.0.0",
      "sha256": "4444444444444444444444444444444444444444444444444444444444444444"
    }
  ],
  "guidance": "hoh resume --run-id 20260908T010203Z-0123456789ab",
  "status": "budget_exhausted",
  "loops_completed": 1
}
```

`run-summary.md` deterministically renders these fields as: run ID, status,
reason, start/current/best candidate, completed loops, elapsed seconds, total
tokens, per-role attempts and usage, verified claims, remaining gaps,
infrastructure/protocol failures, skills, and next-action guidance. No model is
asked to summarize the run.
