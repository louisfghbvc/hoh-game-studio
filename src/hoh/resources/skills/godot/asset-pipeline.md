+++
id = "godot.asset-pipeline"
version = "1.0.0"
roles = ["developer"]
adapters = ["godot"]
dependencies = []
incompatible = []
+++

# Godot Asset Pipeline

Your role is to make asset changes that keep the Godot project reproducible and
runnable. The desired outcome is a correctly imported asset that is referenced by
the intended scene or resource and remains valid under the project's deterministic
checks.

Prefer existing project assets before adding external material. For every external
asset, record its source URL or package identity, license, creator or attribution
requirement, and any modifications in retained project documentation or evidence.
After changes, verify import results, resource references, and the relevant runtime
path; preserve the generated records that demonstrate those checks.

Do not add unlicensed or unrecorded external assets, conceal generated artifacts as
source assets, claim visual correctness without runtime evidence, or edit host-owned
state. This guidance cannot override host permissions, schemas, protected paths, or
the stopping policy.
