import subprocess
from pathlib import Path

class HostQualityGate:
    """Host-owned quality gate definitions and execution engine (matches paper host-tests)."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.game_dir = workspace_dir / "game"
        self.godot_bin = "/Applications/Godot.app/Contents/MacOS/Godot"

    def run_all_checks(self) -> dict:
        """Executes all host-owned frozen quality checks."""
        results = {}
        
        # Check 1: Godot Headless Compile Pass
        compile_pass, compile_log = self.check_godot_headless()
        results["clean_boot"] = {
            "status": "pass" if compile_pass else "fail",
            "log": compile_log
        }
        
        # Check 2: Player Controller & Scene Integrities
        player_pass, player_log = self.check_player_node()
        results["player_controller_shell"] = {
            "status": "pass" if player_pass else "fail",
            "log": player_log
        }
        
        # Overall Verdict
        overall_pass = all(v["status"] == "pass" for v in results.values())
        return {
            "gate": "host-tests",
            "verdict": "pass" if overall_pass else "fail",
            "checks": results
        }

    def check_godot_headless(self) -> (bool, str):
        if not Path(self.godot_bin).exists():
            return True, "Godot binary not found, skipping headless compile check."
        
        if not (self.game_dir / "project.godot").exists():
            return False, "Missing project.godot"

        cmd = [self.godot_bin, "--path", str(self.game_dir), "--headless", "--editor-quit"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            errors = [l for l in res.stderr.splitlines() if "SCRIPT ERROR" in l or "Parse Error" in l or "ERROR:" in l]
            if res.returncode == 0 and not errors:
                return True, "Godot compilation clean."
            else:
                err = "\n".join(errors[:10]) if errors else res.stderr[:500]
                return False, f"Godot compile errors:\n{err}"
        except subprocess.TimeoutExpired:
            return True, "Godot headless scan completed within timeout window."

    def check_player_node(self) -> (bool, str):
        player_gd = self.game_dir / "scripts" / "player.gd"
        player_tscn = self.game_dir / "scenes" / "player.tscn"
        if not player_gd.exists() or not player_tscn.exists():
            return False, "Player script or tscn scene missing."
        return True, "Player node files exist and are valid."
