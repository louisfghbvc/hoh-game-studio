import json
from pathlib import Path

import pytest

from hoh.models import Phase
from hoh.state.store import (
    RunLock,
    RunLockedError,
    StateConflictError,
    StateStore,
    atomic_write_json,
)


def test_atomic_write_json_replaces_a_complete_previous_document(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")

    atomic_write_json(path, {"new": "state"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"new": "state"}


def test_complete_phase_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.create_run("run-1", "abc123")
    store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "p.json"})
    first = store.phase_state("run-1", 1)

    store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "p.json"})

    assert store.phase_state("run-1", 1) == first
    assert first["completed"]["planning"]["idempotency_key"] == "run-1:1:planning"


def test_complete_phase_rejects_a_different_repeated_payload(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.create_run("run-1", "abc123")
    store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "first.json"})

    with pytest.raises(StateConflictError):
        store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "other.json"})


def test_create_run_rejects_a_path_traversal_run_id(tmp_path: Path) -> None:
    store = StateStore(tmp_path)

    with pytest.raises(ValueError, match="single path component"):
        store.create_run("..", "abc123")


def test_completed_phases_are_journaled_in_phase_order(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.create_run("run-1", "abc123")
    store.complete_phase("run-1", 1, Phase.QA, {"report": "qa.json"})
    store.complete_phase("run-1", 1, Phase.PLANNING, {"plan": "p.json"})

    state = store.phase_state("run-1", 1)

    assert list(state["completed"]) == ["planning", "qa"]
    assert store.first_incomplete_phase("run-1", 1) is Phase.PREFLIGHT


def test_second_lock_is_rejected(tmp_path: Path) -> None:
    first = RunLock(tmp_path / ".hoh" / "lock")
    second = RunLock(tmp_path / ".hoh" / "lock")
    first.acquire()

    with pytest.raises(RunLockedError):
        second.acquire()

    first.release()


def test_lock_records_the_owning_process_and_release_removes_it(tmp_path: Path) -> None:
    lock_path = tmp_path / ".hoh" / "lock"
    lock = RunLock(lock_path)

    lock.acquire()

    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert metadata["pid"] > 0
    assert metadata["created_at"].endswith("+00:00")

    lock.release()

    assert not lock_path.exists()
