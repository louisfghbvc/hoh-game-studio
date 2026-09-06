import json
import time
from pathlib import Path

class IssueLedger:
    """Persistent Bug & Regression Ledger matching arXiv:2609.01481 issue_ledger schema."""

    def __init__(self, workspace_dir: Path):
        self.ledger_file = workspace_dir / ".gameloop" / "issue_ledger.json"
        self.init_ledger()

    def init_ledger(self):
        if not self.ledger_file.exists():
            data = {
                "schema_version": 1,
                "summary": {
                    "open": 0,
                    "closed": 0,
                    "regressed": 0,
                    "blocked": 0
                },
                "issues": []
            }
            self.save_ledger(data)

    def load_ledger(self) -> dict:
        return json.loads(self.ledger_file.read_text())

    def save_ledger(self, data: dict):
        # Update summary counts
        counts = {"open": 0, "closed": 0, "regressed": 0, "blocked": 0}
        for iss in data.get("issues", []):
            st = iss.get("status", "open")
            if st in counts:
                counts[st] += 1
        data["summary"] = counts
        self.ledger_file.write_text(json.dumps(data, indent=2))

    def add_issue(self, title: str, description: str, loop_found: int) -> dict:
        data = self.load_ledger()
        issue_id = f"issue:{len(data['issues']) + 1:03d}"
        issue = {
            "id": issue_id,
            "title": title,
            "description": description,
            "status": "open",
            "loop_found": loop_found,
            "loop_closed": None,
            "history": [{
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "open",
                "notes": f"Discovered in Loop {loop_found}"
            }]
        }
        data["issues"].append(issue)
        self.save_ledger(data)
        return issue

    def close_issue(self, issue_id: str, loop_closed: int, notes: str = ""):
        data = self.load_ledger()
        for iss in data["issues"]:
            if iss["id"] == issue_id:
                iss["status"] = "closed"
                iss["loop_closed"] = loop_closed
                iss["history"].append({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "closed",
                    "notes": notes
                })
        self.save_ledger(data)

    def mark_regressed(self, issue_id: str, loop_id: int, notes: str = ""):
        data = self.load_ledger()
        for iss in data["issues"]:
            if iss["id"] == issue_id:
                iss["status"] = "regressed"
                iss["history"].append({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "regressed",
                    "notes": notes
                })
        self.save_ledger(data)
