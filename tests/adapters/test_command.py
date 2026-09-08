from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hoh.adapters.base import AdapterContext
from hoh.adapters.command import CommandAdapter
from hoh.models import CheckBundle


def test_successful_command_records_retained_outputs(tmp_path: Path) -> None:
    """Removing captured command output files must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ok')"),),
        timeout_seconds=5,
    )

    context = _context(tmp_path)
    bundle = adapter.check(context, {})

    assert bundle.status == "pass"
    assert bundle.results[0].status == "pass"
    assert (context.output / "checks" / "command-0001.stdout.txt").read_text(
        encoding="utf-8"
    ) == "ok\n"
    assert (context.output / "checks" / "command-0001.stderr.txt").is_file()
    assert (context.output / "checks" / "command-0001.json").is_file()


def test_nonzero_exit_fails_candidate(tmp_path: Path) -> None:
    """Treating a nonzero exit as a pass must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "import sys; sys.exit(7)"),),
        timeout_seconds=5,
    )

    assert adapter.check(_context(tmp_path), {}).status == "fail"


def test_timeout_is_failure_not_pass(tmp_path: Path) -> None:
    """Removing timeout failure handling must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "import time; time.sleep(2)"),),
        timeout_seconds=1,
    )

    assert adapter.check(_context(tmp_path), {}).status == "fail"


def test_timeout_terminates_descendant_and_returns_before_child_lifetime(
    tmp_path: Path,
) -> None:
    """Killing only the configured parent must leave inherited pipes blocking."""

    child_pid_path = tmp_path / "child.pid"
    command = (
        sys.executable,
        "-c",
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], "
        "stdout=sys.stdout, stderr=sys.stderr); "
        f"open({json.dumps(str(child_pid_path))}, 'w', encoding='utf-8').write(str(child.pid)); "
        "time.sleep(4)",
    )
    adapter = CommandAdapter(checks=(command,), timeout_seconds=1)

    started = time.monotonic()
    bundle = adapter.check(_context(tmp_path), {})
    elapsed = time.monotonic() - started

    assert bundle.status == "fail"
    assert elapsed < 3.5
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert _wait_until_process_exits(child_pid)
    metadata = json.loads(
        (_context(tmp_path).output / "checks" / "command-0001.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["timed_out"] is True


def test_missing_required_artifact_fails_candidate(tmp_path: Path) -> None:
    """Dropping required-artifact validation must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ok')"),),
        required_artifacts=("evidence/telemetry.jsonl",),
        timeout_seconds=5,
    )

    bundle = adapter.check(_context(tmp_path), {})

    assert bundle.status == "fail"
    assert bundle.results[-1].check_id == "artifact:evidence/telemetry.jsonl"


def test_error_pattern_in_successful_command_output_fails_candidate(tmp_path: Path) -> None:
    """Removing output pattern matching must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ERROR: bad runtime')"),),
        error_patterns=(r"ERROR:",),
        timeout_seconds=5,
    )

    bundle = adapter.check(_context(tmp_path), {})

    assert bundle.status == "fail"
    assert bundle.results[0].summary == "Command output matched error pattern: ERROR:"


def test_missing_executable_blocks_candidate(tmp_path: Path) -> None:
    """Converting a missing executable into pass or fail must make this fail."""
    missing = tmp_path / "missing-command"
    adapter = CommandAdapter(checks=((str(missing),),), timeout_seconds=5)

    bundle = adapter.check(_context(tmp_path), {})

    assert bundle.status == "blocked"
    assert bundle.results[0].status == "blocked"
    assert adapter.doctor(tmp_path)[0].severity == "blocked"


def test_collect_copies_only_configured_artifacts_and_hashes_them(tmp_path: Path) -> None:
    """Copying unconfigured files or omitting the hash must make this fail."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "telemetry.jsonl").write_bytes(b"event\n")
    (evidence / "private.txt").write_text("do not collect", encoding="utf-8")
    context = _context(tmp_path)
    adapter = CommandAdapter(artifact_globs=("evidence/*.jsonl",))

    artifacts = adapter.collect(context, adapter.check(context, {}))

    assert artifacts == {
        "artifacts/evidence/telemetry.jsonl": (
            "d8073d788ee641f2f54333c3246b08951f721c3f8090cdcb1f0fa9e80eaef504"
        )
    }
    assert (context.output / "artifacts" / "evidence" / "telemetry.jsonl").is_file()
    assert not (context.output / "artifacts" / "evidence" / "private.txt").exists()


def test_summary_uses_tracked_names_without_reading_file_contents(tmp_path: Path) -> None:
    """Counting untracked files or embedding their contents must make this fail."""
    _run_git(tmp_path, "init")
    (tmp_path / "main.py").write_text("secret source", encoding="utf-8")
    (tmp_path / "README").write_text("secret readme", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("untracked", encoding="utf-8")
    _run_git(tmp_path, "add", "main.py", "README")
    _run_git(tmp_path, "-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-m", "initial")
    adapter = CommandAdapter(entrypoints=("main.py",))

    summary = adapter.summarize(tmp_path)

    assert summary["tracked_file_count"] == 2
    assert summary["language_suffix_counts"] == {".py": 1, "<none>": 1}
    assert summary["sha"] == _run_git(tmp_path, "rev-parse", "HEAD")
    assert summary["entrypoints"] == ("main.py",)
    assert "secret source" not in repr(summary)
    assert "ignored.py" not in repr(summary)


@pytest.mark.parametrize("nested_output", [False, True])
def test_candidate_or_nested_output_is_rejected_before_adapter_writes(
    tmp_path: Path, nested_output: bool
) -> None:
    """Allowing host records inside the candidate must make this fail."""
    output = tmp_path / "host-output" if nested_output else tmp_path
    context = AdapterContext(tmp_path, output)
    adapter = CommandAdapter()
    bundle = CheckBundle("command", "pass", ())

    with pytest.raises(ValueError, match="outside the candidate"):
        adapter.check(context, {})
    with pytest.raises(ValueError, match="outside the candidate"):
        adapter.collect(context, bundle)

    assert not (tmp_path / "host-output" / "checks").exists()
    assert not (tmp_path / "host-output" / "artifacts").exists()


@pytest.mark.parametrize("artifact", ["../outside.txt", "{absolute}"])
def test_external_required_artifact_cannot_pass(
    tmp_path: Path, artifact: str
) -> None:
    """Allowing absolute or traversal artifact paths to pass must make this fail."""
    external = tmp_path.parent / "outside.txt"
    external.write_text("outside", encoding="utf-8")
    configured = str(external) if artifact == "{absolute}" else artifact
    adapter = CommandAdapter(required_artifacts=(configured,))

    bundle = adapter.check(_context(tmp_path), {})

    assert bundle.status == "fail"
    assert bundle.results[-1].status == "fail"


def test_required_artifact_symlink_escape_cannot_pass(tmp_path: Path) -> None:
    """Following a required-artifact symlink out of the candidate must fail."""
    external = tmp_path.parent / "outside.txt"
    external.write_text("outside", encoding="utf-8")
    escape = tmp_path / "escape.txt"
    try:
        escape.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    adapter = CommandAdapter(required_artifacts=("escape.txt",))

    bundle = adapter.check(_context(tmp_path), {})

    assert bundle.status == "fail"
    assert bundle.results[-1].status == "fail"


def _run_git(project: Path, *arguments: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *arguments],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _context(project: Path) -> AdapterContext:
    return AdapterContext(project, project.parent / f"{project.name}-host-output")


def _wait_until_process_exits(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        return f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
