import os
import json
import hashlib
import subprocess
from pathlib import Path

class VCSManager:
    """Manages Candidate IDs, Git Tree SHA256 Hashes, Two-Branch Flow (gameloop -> main), and Rollbacks."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.gameloop_dir = workspace_dir / ".gameloop"
        self.gameloop_dir.mkdir(parents=True, exist_ok=True)
        self.vcs_file = self.gameloop_dir / "vcs.json"
        self.init_vcs()

    def init_vcs(self):
        if not (self.workspace_dir / ".git").exists():
            subprocess.run(["git", "init"], cwd=self.workspace_dir, check=True)
            # Create gameloop development branch
            subprocess.run(["git", "checkout", "-b", "gameloop"], cwd=self.workspace_dir, check=True)
        if not self.vcs_file.exists():
            data = {
                "schema_version": 1,
                "default_bookmark": "main",
                "development_bookmark": "gameloop",
                "game_name": "VoidKnight",
                "game_source_root": "game",
                "push_policy": "host_gate_pass_to_development_branch;qa_pass_to_main",
                "roles": {
                    "planner": "gameloop-planner-bot[bot] <planner@hoh.local>",
                    "developer": "gameloop-developer-bot[bot] <developer@hoh.local>",
                    "tester": "gameloop-tester-bot[bot] <tester@hoh.local>"
                },
                "current_loop": 1,
                "head_candidate": None,
                "candidates": {}
            }
            self.save_vcs(data)

    def load_vcs(self) -> dict:
        return json.loads(self.vcs_file.read_text())

    def save_vcs(self, data: dict):
        self.vcs_file.write_text(json.dumps(data, indent=2))

    def compute_tree_sha256(self, directory: Path) -> str:
        """Computes SHA256 tree hash of all files in a directory."""
        hasher = hashlib.sha256()
        for root, dirs, files in sorted(os.walk(directory)):
            dirs.sort()
            for f in sorted(files):
                if ".git" in root or ".godot" in root:
                    continue
                p = Path(root) / f
                try:
                    hasher.update(str(p.relative_to(directory)).encode())
                    hasher.update(p.read_bytes())
                except Exception:
                    pass
        return hasher.hexdigest()

    def create_candidate(self, loop_id: int, role: str) -> dict:
        vcs_data = self.load_vcs()
        tree_hash = self.compute_tree_sha256(self.workspace_dir / "game")
        cand_id = f"loop-{loop_id:02d}-{tree_hash[:12]}"
        candidate_info = {
            "candidate_id": cand_id,
            "loop_index": loop_id,
            "role": role,
            "tree_sha256": tree_hash,
            "git_commit": self.get_current_git_commit()
        }
        vcs_data["current_loop"] = loop_id
        vcs_data["head_candidate"] = cand_id
        vcs_data["candidates"][cand_id] = candidate_info
        self.save_vcs(vcs_data)
        return candidate_info

    def get_current_git_commit(self) -> str:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workspace_dir, capture_output=True, text=True)
        return res.stdout.strip() if res.returncode == 0 else "initial"

    def commit_to_gameloop(self, loop_id: int, summary: str, role: str = "developer"):
        """Commits to development branch (gameloop) upon Host Gate pass."""
        author = f"gameloop-{role}-bot[bot] <{role}@hoh.local>"
        msg = f"feat(loop-{loop_id:04d}): {summary}"
        subprocess.run(["git", "add", "."], cwd=self.workspace_dir)
        subprocess.run(["git", "commit", "-m", msg, "--author", author, "--allow-empty"], cwd=self.workspace_dir)

    def merge_to_main(self, loop_id: int):
        """Merges gameloop branch into main branch upon QA Tester final approval."""
        subprocess.run(["git", "checkout", "main"], cwd=self.workspace_dir, capture_output=True)
        res = subprocess.run(["git", "merge", "gameloop", "--no-ff", "-m", f"release(loop-{loop_id:04d}): QA Approved Release"], cwd=self.workspace_dir, capture_output=True)
        if res.returncode != 0:
            # Resolve any conflict by taking gameloop version
            subprocess.run(["git", "checkout", "--theirs", "."], cwd=self.workspace_dir, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=self.workspace_dir, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"release(loop-{loop_id:04d}): QA Approved Release"], cwd=self.workspace_dir, capture_output=True)
        subprocess.run(["git", "checkout", "gameloop"], cwd=self.workspace_dir, capture_output=True)

    def rollback_to_commit(self, commit_hash: str):
        subprocess.run(["git", "reset", "--hard", commit_hash], cwd=self.workspace_dir)
        subprocess.run(["git", "clean", "-fd"], cwd=self.workspace_dir)
