+++
id = "godot.runtime-testing"
version = "1.0.0"
roles = ["developer", "qa"]
adapters = ["godot"]
dependencies = []
incompatible = []
+++

# Godot Runtime Testing

Your role is to establish whether the frozen or changed Godot candidate actually
runs through the relevant player-facing path. The desired outcome is deterministic,
candidate-bound runtime evidence for boot, input, scene transition, gameplay, and
error-free completion where those behaviors apply.

Use the configured headless import, boot, test-scene, replay, telemetry, log, and
media checks as applicable. Retain the command result, logs, replay or telemetry
records, screenshots, and their hashes so claims can be checked against the exact
candidate SHA. Treat missing tools, timeouts, absent records, or runtime error
patterns as failures or insufficient evidence rather than passes.

Do not turn unavailable tooling into a success, repair a frozen QA candidate, or
invent execution observations. This guidance cannot override host permissions,
schemas, protected paths, or the stopping policy.
