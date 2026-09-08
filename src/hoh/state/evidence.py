"""Host-owned validation and normalization of candidate-bound QA evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path, PureWindowsPath


class EvidenceBindingError(ValueError):
    """Raised when QA evidence cannot be bound to host-owned facts."""


_GAP_SEVERITIES = frozenset({"blocker", "major", "minor"})


class EvidenceNormalizer:
    """Bind schema-valid QA evidence to a candidate and retained artifacts."""

    def __init__(
        self,
        evidence_root: Path,
        requirements: Mapping[str, object] | Sequence[Mapping[str, object]] | Path | None = None,
    ) -> None:
        self._evidence_root = Path(evidence_root).resolve()
        self._requirements = requirements

    def normalize(
        self,
        raw: Mapping[str, object],
        candidate_sha: str,
        artifact_tree_sha256: str,
        adapter_manifest: Mapping[str, object],
        requirements: Mapping[str, object] | Sequence[Mapping[str, object]] | Path | None = None,
    ) -> dict[str, object]:
        """Return deterministic evidence after verifying every host-owned binding."""

        if raw.get("candidate_sha") != candidate_sha:
            raise EvidenceBindingError("candidate_sha does not match the frozen candidate")
        if raw.get("artifact_tree_sha256") != artifact_tree_sha256:
            raise EvidenceBindingError(
                "artifact_tree_sha256 does not match the frozen candidate"
            )

        artifacts, checks_passed, diagnostics = _manifest_parts(adapter_manifest)
        verified_records = _records(raw, "verified_records")
        gap_records = _records(raw, "gap_records")
        for record in verified_records:
            execution_records = record.get("execution_records")
            if not isinstance(execution_records, list):
                raise EvidenceBindingError("execution_records must be a list")
            for execution_record in execution_records:
                if not isinstance(execution_record, Mapping):
                    raise EvidenceBindingError("execution record must be an object")
                self._verify_execution_record(execution_record, artifacts)

        registry = requirements if requirements is not None else self._requirements
        known_claim_ids, required_claim_ids = _claim_registry(registry)
        all_records = [*verified_records, *gap_records]
        claim_ids = [_claim_id(record) for record in all_records]
        duplicates = sorted(
            claim_id for claim_id in set(claim_ids) if claim_ids.count(claim_id) > 1
        )
        if duplicates:
            raise EvidenceBindingError(
                "duplicate claim_id records: " + ", ".join(duplicates)
            )
        if known_claim_ids is not None:
            unknown = sorted(set(claim_ids) - known_claim_ids)
            if unknown:
                raise EvidenceBindingError("unknown claim_id records: " + ", ".join(unknown))

        normalized = deepcopy(dict(raw))
        normalized_verified = sorted(
            deepcopy(verified_records), key=lambda record: _claim_id(record)
        )
        for record in normalized_verified:
            execution_records = record.get("execution_records")
            assert isinstance(execution_records, list)
            record["execution_records"] = sorted(
                execution_records,
                key=lambda item: (
                    str(item.get("path", "")) if isinstance(item, Mapping) else "",
                    str(item.get("type", "")) if isinstance(item, Mapping) else "",
                ),
            )
        normalized_gaps = sorted(
            deepcopy(gap_records), key=lambda record: _claim_id(record)
        )
        verified_claim_ids = {_claim_id(record) for record in verified_records}
        verified_required_claim_ids = required_claim_ids & verified_claim_ids
        gap_severities = [_gap_severity(record) for record in gap_records]
        has_blocking_gap = any(
            severity in {"blocker", "major"} for severity in gap_severities
        )
        product_complete = (
            required_claim_ids <= verified_claim_ids
            and not has_blocking_gap
            and checks_passed
        )

        normalized["verified_records"] = normalized_verified
        normalized["gap_records"] = normalized_gaps
        normalized["product_complete"] = product_complete
        normalized["host_metadata"] = {
            "qa_product_complete": raw.get("product_complete") is True,
            "deterministic_checks_passed": checks_passed,
            "required_claim_ids": sorted(required_claim_ids),
            "verified_required_claim_ids": sorted(verified_required_claim_ids),
        }
        normalized["diagnostics"] = diagnostics
        return normalized

    def _verify_execution_record(
        self, record: Mapping[str, object], manifest: Mapping[str, str]
    ) -> None:
        raw_path = record.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise EvidenceBindingError("execution record path must be a non-empty string")
        relative = Path(raw_path)
        if relative.is_absolute() or PureWindowsPath(raw_path).drive:
            raise EvidenceBindingError(f"execution record path is outside evidence root: {raw_path}")
        try:
            artifact = (self._evidence_root / relative).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise EvidenceBindingError(f"invalid execution record path: {raw_path}") from error
        if not artifact.is_relative_to(self._evidence_root):
            raise EvidenceBindingError(f"execution record path is outside evidence root: {raw_path}")
        if raw_path not in manifest:
            raise EvidenceBindingError(
                f"execution record path is absent from adapter manifest: {raw_path}"
            )
        if not artifact.is_file():
            raise EvidenceBindingError(f"cited evidence file does not exist: {raw_path}")

        actual_sha256 = _sha256(artifact)
        qa_sha256 = record.get("sha256")
        manifest_sha256 = manifest[raw_path]
        if qa_sha256 != actual_sha256:
            raise EvidenceBindingError(f"execution record sha256 does not match file: {raw_path}")
        if manifest_sha256 != actual_sha256:
            raise EvidenceBindingError(f"adapter manifest sha256 does not match file: {raw_path}")


def _manifest_parts(
    manifest: Mapping[str, object],
) -> tuple[dict[str, str], bool, list[object]]:
    if not isinstance(manifest, Mapping):
        raise EvidenceBindingError("adapter manifest must be an object")
    nested_artifacts = manifest.get("artifacts")
    if nested_artifacts is not None:
        if not isinstance(nested_artifacts, Mapping):
            raise EvidenceBindingError("adapter manifest artifacts must be an object")
        artifact_values = nested_artifacts
        checks_passed = manifest.get("status") == "pass"
        raw_diagnostics = manifest.get("diagnostics", [])
    else:
        artifact_values = manifest
        checks_passed = False
        raw_diagnostics = []
    artifacts: dict[str, str] = {}
    for path, sha256 in artifact_values.items():
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise EvidenceBindingError("adapter manifest artifact hashes must be strings")
        artifacts[path] = sha256
    if not isinstance(raw_diagnostics, list):
        raise EvidenceBindingError("adapter manifest diagnostics must be a list")
    diagnostics = sorted(
        deepcopy(raw_diagnostics),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )
    return artifacts, checks_passed, diagnostics


def _claim_registry(
    registry: Mapping[str, object] | Sequence[Mapping[str, object]] | Path | None,
) -> tuple[set[str] | None, set[str]]:
    if registry is None:
        raise EvidenceBindingError("requirements registry is required")
    if isinstance(registry, Path):
        try:
            loaded = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EvidenceBindingError(f"could not read requirements registry: {registry}") from error
        registry = loaded
    if isinstance(registry, Mapping):
        claims = registry.get("claims")
    elif isinstance(registry, Sequence) and not isinstance(
        registry, (str, bytes, bytearray)
    ):
        claims = registry
    else:
        claims = None
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes, bytearray)):
        raise EvidenceBindingError("requirements registry claims must be a list")

    known: set[str] = set()
    required: set[str] = set()
    duplicates: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise EvidenceBindingError("requirements registry claim must be an object")
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            raise EvidenceBindingError("requirements registry claim id must be non-empty")
        if claim_id in known:
            duplicates.add(claim_id)
        known.add(claim_id)
        if claim.get("required") is True:
            required.add(claim_id)
    if duplicates:
        raise EvidenceBindingError(
            "duplicate claim_id values in requirements registry: "
            + ", ".join(sorted(duplicates))
        )
    return known, required


def _records(raw: Mapping[str, object], field: str) -> list[dict[str, object]]:
    records = raw.get(field)
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise EvidenceBindingError(f"{field} must be a list of objects")
    return records


def _claim_id(record: Mapping[str, object]) -> str:
    claim_id = record.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        raise EvidenceBindingError("claim_id must be a non-empty string")
    return claim_id


def _gap_severity(record: Mapping[str, object]) -> str:
    severity = record.get("severity")
    if not isinstance(severity, str) or severity not in _GAP_SEVERITIES:
        raise EvidenceBindingError(
            "gap severity must be one of: blocker, major, minor"
        )
    return severity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
