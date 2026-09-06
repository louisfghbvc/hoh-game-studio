#!/usr/bin/env python3
"""
Harness-of-Harness (HoH) Automated Game Development Orchestrator
-----------------------------------------------------------------
This script automates the Plan-Code-Test loop for creating games using LLM Agents.
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path

WORKSPACE = Path(__file__).parent.resolve()
GAME_DIR = WORKSPACE / "game"
RECEIPTS_DIR = WORKSPACE / ".gameloop" / "receipts"
EVENTS_DIR = WORKSPACE / ".gameloop" / "events"

def init_workspace():
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    if not (WORKSPACE / ".git").exists():
        subprocess.run(["git", "init"], cwd=WORKSPACE, check=True)
        print("Initialized Git repository.")

def run_godot_headless_check():
    """Runs Godot in headless mode to verify GDScript syntax and scene integrity."""
    godot_bin = "/Applications/Godot.app/Contents/MacOS/Godot"
    if not Path(godot_bin).exists():
        return True, "Godot binary not found at default location. Skipping syntax check."
    
    if not (GAME_DIR / "project.godot").exists():
        return False, "project.godot missing in game/ folder."

    cmd = [godot_bin, "--path", str(GAME_DIR), "--headless", "--editor-quit"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    
    errors = [line for line in res.stderr.splitlines() if "SCRIPT ERROR" in line or "Parse Error" in line or "ERROR:" in line]
    if res.returncode == 0 and not errors:
        return True, "Godot headless compilation passed clean."
    else:
        err_msg = "\n".join(errors[:10]) if errors else res.stderr[:500]
        return False, f"Godot check found issues:\n{err_msg}"

def log_receipt(loop_id, status, notes):
    receipt = {
        "loop_id": loop_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "notes": notes
    }
    receipt_file = RECEIPTS_DIR / f"receipt_loop_{loop_id:03d}.json"
    receipt_file.write_text(json.dumps(receipt, indent=2))
    print(f"Recorded receipt for Loop {loop_id} -> {receipt_file.name}")

def commit_loop(loop_id, task_summary):
    msg = f"hoh(loop-{loop_id:03d}): {task_summary}"
    subprocess.run(["git", "add", "."], cwd=WORKSPACE)
    subprocess.run(["git", "commit", "-m", msg, "--allow-empty"], cwd=WORKSPACE)
    print(f"Git commit created: '{msg}'")

def main():
    init_workspace()
    print("==================================================")
    print("  Harness-of-Harness (HoH) Game Studio Initialized")
    print("==================================================")
    print(f"Workspace: {WORKSPACE}")
    print("Ready to run Planner -> Developer -> QA Tester loops!")

if __name__ == "__main__":
    main()
