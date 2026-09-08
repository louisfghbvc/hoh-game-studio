"""A deterministic stand-in for the Godot command line used by adapter tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path


def main() -> int:
    arguments = sys.argv[1:]
    if "--path" not in arguments:
        print("ERROR: missing Godot project path", file=sys.stderr)
        return 2
    project = Path(arguments[arguments.index("--path") + 1])
    project_config = (project / "project.godot").read_text(encoding="utf-8")
    if "fake_sleep = true" in project_config:
        time.sleep(2)
    if "fake_crash = true" in project_config:
        print("ERROR: fake runtime crash", file=sys.stderr)
        return 9
    if "fake_error = true" in project_config:
        print("SCRIPT ERROR: fake runtime error", file=sys.stderr)
    print("fake Godot booted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
