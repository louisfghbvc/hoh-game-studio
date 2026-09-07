from __future__ import annotations

import sys
from pathlib import Path

from hoh.adapters.base import AdapterContext
from hoh.adapters.command import CommandAdapter


def test_successful_command_records_retained_outputs(tmp_path: Path) -> None:
    """Removing captured command output files must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ok')"),),
        timeout_seconds=5,
    )

    bundle = adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {})

    assert bundle.status == "pass"
    assert bundle.results[0].status == "pass"
    assert (tmp_path / "out" / "checks" / "command-0001.stdout.txt").read_text(
        encoding="utf-8"
    ) == "ok\n"
    assert (tmp_path / "out" / "checks" / "command-0001.stderr.txt").is_file()
    assert (tmp_path / "out" / "checks" / "command-0001.json").is_file()


def test_nonzero_exit_fails_candidate(tmp_path: Path) -> None:
    """Treating a nonzero exit as a pass must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "import sys; sys.exit(7)"),),
        timeout_seconds=5,
    )

    assert adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {}).status == "fail"


def test_timeout_is_failure_not_pass(tmp_path: Path) -> None:
    """Removing timeout failure handling must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "import time; time.sleep(2)"),),
        timeout_seconds=1,
    )

    assert adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {}).status == "fail"


def test_missing_required_artifact_fails_candidate(tmp_path: Path) -> None:
    """Dropping required-artifact validation must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ok')"),),
        required_artifacts=("evidence/telemetry.jsonl",),
        timeout_seconds=5,
    )

    bundle = adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {})

    assert bundle.status == "fail"
    assert bundle.results[-1].check_id == "artifact:evidence/telemetry.jsonl"


def test_error_pattern_in_successful_command_output_fails_candidate(tmp_path: Path) -> None:
    """Removing output pattern matching must make this fail."""
    adapter = CommandAdapter(
        checks=((sys.executable, "-c", "print('ERROR: bad runtime')"),),
        error_patterns=(r"ERROR:",),
        timeout_seconds=5,
    )

    bundle = adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {})

    assert bundle.status == "fail"
    assert bundle.results[0].summary == "Command output matched error pattern: ERROR:"


def test_missing_executable_blocks_candidate(tmp_path: Path) -> None:
    """Converting a missing executable into pass or fail must make this fail."""
    missing = tmp_path / "missing-command"
    adapter = CommandAdapter(checks=((str(missing),),), timeout_seconds=5)

    bundle = adapter.check(AdapterContext(tmp_path, tmp_path / "out"), {})

    assert bundle.status == "blocked"
    assert bundle.results[0].status == "blocked"
    assert adapter.doctor(tmp_path)[0].severity == "blocked"


def test_collect_copies_only_configured_artifacts_and_hashes_them(tmp_path: Path) -> None:
    """Copying unconfigured files or omitting the hash must make this fail."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "telemetry.jsonl").write_text("event\n", encoding="utf-8")
    (evidence / "private.txt").write_text("do not collect", encoding="utf-8")
    context = AdapterContext(tmp_path, tmp_path / "out")
    adapter = CommandAdapter(artifact_globs=("evidence/*.jsonl",))

    artifacts = adapter.collect(context, adapter.check(context, {}))

    assert artifacts == {
        "artifacts/evidence/telemetry.jsonl": (
            "627f0173e2a3c6a8b2019573d8f9d1ddb9cc4b650087e7cb43af28e1c23763ba"
        )
    }
    assert (tmp_path / "out" / "artifacts" / "evidence" / "telemetry.jsonl").is_file()
    assert not (tmp_path / "out" / "artifacts" / "evidence" / "private.txt").exists()


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


def _run_git(project: Path, *arguments: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *arguments],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
