"""Durable, host-owned state for resumable HoH runs."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from hoh.models import Phase


class StateError(RuntimeError):
    """Base error for durable run state."""


class StateConflictError(StateError):
    """Raised when an idempotency key is reused with different data."""


class StateNotFoundError(StateError):
    """Raised when a requested run does not exist."""


class StateCompleteError(StateError):
    """Raised when every phase in a loop has completed."""


class RunLockedError(StateError):
    """Raised when another run owns the product lock."""


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Replace *path* only after a fully flushed JSON document is ready."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2, allow_nan=False)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


class StateStore:
    """Journal phase completions beneath a project's ``.hoh`` directory."""

    def __init__(self, project: Path) -> None:
        self._project = Path(project)
        self._state_directory = self._project / ".hoh"
        self._runs_directory = self._state_directory / "runs"

    def create_run(self, run_id: str, start_sha: str) -> Path:
        """Create a run once, or verify that a repeated creation matches it."""

        run_directory = self._run_directory(run_id)
        run_directory.mkdir(parents=True, exist_ok=True)
        metadata_path = run_directory / "run.json"
        metadata = {"schema_version": 1, "run_id": run_id, "start_sha": start_sha}
        if metadata_path.exists():
            existing = self._read_json(metadata_path)
            if existing.get("run_id") != run_id or existing.get("start_sha") != start_sha:
                raise StateConflictError(f"run {run_id!r} already has different metadata")
            return run_directory

        atomic_write_json(metadata_path, metadata)
        return run_directory

    def phase_state(self, run_id: str, loop_index: int) -> dict:
        """Return the durable phase journal for a run loop."""

        self._require_run(run_id)
        path = self._phase_path(run_id, loop_index)
        if not path.exists():
            return {
                "schema_version": 1,
                "run_id": run_id,
                "loop_index": loop_index,
                "completed": {},
            }
        state = self._read_json(path)
        completed = state.get("completed")
        if not isinstance(completed, dict):
            raise StateError(f"phase journal for run {run_id!r} is invalid")
        state["completed"] = self._ordered_completed(completed)
        return state

    def complete_phase(
        self,
        run_id: str,
        loop_index: int,
        phase: Phase,
        payload: Mapping[str, object],
    ) -> None:
        """Record a completed phase, rejecting conflicting replays."""

        state = self.phase_state(run_id, loop_index)
        completed = state["completed"]
        phase_name = phase.value
        normalized_payload = self._normalize_payload(payload)
        idempotency_key = f"{run_id}:{loop_index}:{phase_name}"
        existing = completed.get(phase_name)
        if existing is not None:
            if not isinstance(existing, dict):
                raise StateError(f"phase journal for run {run_id!r} is invalid")
            if (
                existing.get("idempotency_key") == idempotency_key
                and existing.get("payload") == normalized_payload
            ):
                return
            raise StateConflictError(f"phase {idempotency_key!r} already has different data")

        completed[phase_name] = {
            "idempotency_key": idempotency_key,
            "payload": normalized_payload,
        }
        state["completed"] = self._ordered_completed(completed)
        atomic_write_json(self._phase_path(run_id, loop_index), state)

    def first_incomplete_phase(self, run_id: str, loop_index: int) -> Phase:
        """Return the first incomplete phase in the model's fixed phase order."""

        completed = self.phase_state(run_id, loop_index)["completed"]
        for phase in Phase:
            if phase.value not in completed:
                return phase
        raise StateCompleteError(f"all phases are complete for run {run_id!r}, loop {loop_index}")

    def _run_directory(self, run_id: str) -> Path:
        if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
            raise ValueError("run_id must be a non-empty single path component")
        candidate = self._runs_directory / run_id
        return candidate

    def _require_run(self, run_id: str) -> None:
        metadata_path = self._run_directory(run_id) / "run.json"
        if not metadata_path.is_file():
            raise StateNotFoundError(f"run {run_id!r} does not exist")

    def _phase_path(self, run_id: str, loop_index: int) -> Path:
        if loop_index < 1:
            raise ValueError("loop_index must be positive")
        return (
            self._run_directory(run_id)
            / "loops"
            / f"loop-{loop_index:04d}"
            / "phase-state.json"
        )

    @staticmethod
    def _normalize_payload(payload: Mapping[str, object]) -> dict:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        return json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))

    @staticmethod
    def _ordered_completed(completed: Mapping[str, object]) -> dict:
        return {phase.value: completed[phase.value] for phase in Phase if phase.value in completed}

    @staticmethod
    def _read_json(path: Path) -> dict:
        with path.open(encoding="utf-8") as state_file:
            document = json.load(state_file)
        if not isinstance(document, dict):
            raise StateError(f"state document {path} is not an object")
        return document


class RunLock:
    """A product-wide lock that remains intact until its owner releases it."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._metadata: dict[str, object] | None = None

    def acquire(self) -> None:
        """Atomically claim the lock without breaking an existing lock."""

        if self._metadata is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        metadata: dict[str, object] = {
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with self._path.open("x", encoding="utf-8") as lock_file:
                json.dump(metadata, lock_file, ensure_ascii=False, allow_nan=False)
                lock_file.write("\n")
                lock_file.flush()
                os.fsync(lock_file.fileno())
        except FileExistsError as error:
            raise RunLockedError(f"run lock already exists at {self._path}") from error
        self._metadata = metadata

    def release(self) -> None:
        """Release a lock owned by this instance without deleting a replacement."""

        if self._metadata is None:
            return
        try:
            if self._path.exists() and self._read_metadata() == self._metadata:
                self._path.unlink()
        finally:
            self._metadata = None

    def _read_metadata(self) -> dict[str, object]:
        with self._path.open(encoding="utf-8") as lock_file:
            document = json.load(lock_file)
        if not isinstance(document, dict):
            return {}
        return document
