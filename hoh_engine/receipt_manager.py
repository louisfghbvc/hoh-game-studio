import json
import hashlib
import time
from pathlib import Path

class ReceiptManager:
    """Generates immutable JSON receipts matching paper's .gameloop/receipts schema."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.receipts_dir = workspace_dir / ".gameloop" / "receipts"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def write_receipt(self, loop_id: int, role: str, payload: dict) -> Path:
        loop_dir = self.receipts_dir / f"loop-{loop_id:04d}"
        loop_dir.mkdir(parents=True, exist_ok=True)

        payload_bytes = json.dumps(payload, sort_keys=True).encode()
        sha_hash = hashlib.sha256(payload_bytes).hexdigest()[:12]
        
        receipt_file = loop_dir / f"{role}-{sha_hash}.json"
        
        full_receipt = {
            "schema_version": 1,
            "role": role,
            "loop_index": loop_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "content_sha256": sha_hash,
            "payload": payload
        }
        receipt_file.write_text(json.dumps(full_receipt, indent=2))
        return receipt_file
