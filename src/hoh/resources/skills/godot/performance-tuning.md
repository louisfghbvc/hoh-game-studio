+++
id = "godot.performance-tuning"
version = "1.0.0"
roles = ["developer"]
adapters = ["godot"]
dependencies = []
incompatible = []
+++

# Godot Performance Tuning

Your role is to make a measured Godot performance improvement without trading away
required behavior. The desired outcome is a targeted change with reproducible
before-and-after evidence for the relevant scene, replay, hardware profile, or
telemetry metric.

Start from retained measurements and identify the bottleneck in rendering, scripts,
physics, loading, memory, or asset use. Keep the workload and measurement conditions
comparable. Record profiler output, telemetry, logs, frame-time records, or other
artifacts with candidate-bound hashes, and rerun the acceptance path to prove that
the optimization preserves observable behavior.

Do not optimize from intuition alone, hide regressions behind aggregate averages, or
alter host-owned state. This guidance cannot override host permissions, schemas,
protected paths, or the stopping policy.
