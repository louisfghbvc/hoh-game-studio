from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hoh.vcs.git import GitError, GitService
from hoh.vcs.worktree import QaWorktree, QaWorktreeCleanupError


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def initialized_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    repo.mkdir()
    run_git(repo, "init", "--initial-branch=main")
    (repo / "product.txt").write_text("original", encoding="utf-8")
    run_git(repo, "add", "product.txt")
    run_git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "initial",
    )
    return repo


def test_candidate_and_detached_worktree_are_bound(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    (repo / "product.txt").write_text("changed", encoding="utf-8")
    candidate = git.commit_candidate(1, "change product")
    worktree_path = repo / ".hoh" / "tmp" / "qa-1"

    with QaWorktree(git, worktree_path) as frozen:
        assert frozen == worktree_path.resolve()
        assert git.rev_parse_in(frozen, "HEAD") == candidate
        assert git.current_branch_in(frozen) == ""

    assert not worktree_path.exists()
    assert str(worktree_path.resolve()) not in run_git(repo, "worktree", "list")


def test_create_can_freeze_an_explicit_older_candidate(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    first = git.head_sha()
    (repo / "product.txt").write_text("changed", encoding="utf-8")
    git.commit_candidate(1, "change product")
    worktree = QaWorktree(git, repo / ".hoh" / "tmp" / "qa-old")

    frozen = worktree.create(first)

    try:
        assert git.rev_parse_in(frozen, "HEAD") == first
        assert git.current_branch_in(frozen) == ""
    finally:
        worktree.remove()


def test_create_excludes_long_tracked_host_state_when_repo_disables_longpaths(
    tmp_path: Path,
) -> None:
    """QA needs the product tree, not recursively nested host evidence."""

    repo = initialized_repo(tmp_path)
    evidence_parent = repo / ".hoh" / "runs" / "run-a" / "loops" / "loop-0001"
    filename_length = 250 - len(str(evidence_parent)) - 1 - len(".json")
    if filename_length <= 0:
        pytest.skip("temporary repository path is already too long for this fixture")
    evidence = evidence_parent / ("r" * filename_length + ".json")
    assert len(str(evidence)) == 250
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}\n", encoding="utf-8")
    run_git(repo, "-c", "core.longpaths=true", "add", ".hoh")
    run_git(
        repo,
        "-c",
        "core.longpaths=true",
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "retain evidence",
    )
    run_git(repo, "config", "core.longpaths", "false")
    worktree_path = repo / ".hoh" / "tmp" / "qa-long"

    with QaWorktree(GitService(repo), worktree_path) as frozen:
        assert (frozen / "product.txt").read_text(encoding="utf-8") == "original"
        assert not (frozen / ".hoh").exists()


@pytest.mark.parametrize("failing_command", ("sparse-checkout", "checkout"))
def test_partial_create_retains_cleanup_ownership_after_first_removal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_command: str,
) -> None:
    """A caller's finally block must be able to retry partial-create cleanup."""

    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    worktree_path = repo / ".hoh" / "tmp" / "qa-partial"
    worktree = QaWorktree(git, worktree_path)
    original_run = git._run
    original_remove = git._remove_worktree
    removal_attempts = 0

    def fail_setup(args, **kwargs):
        if args[0] == failing_command:
            raise GitError(f"simulated {failing_command} failure")
        return original_run(args, **kwargs)

    def fail_first_removal(path: Path) -> None:
        nonlocal removal_attempts
        removal_attempts += 1
        if removal_attempts == 1:
            raise GitError("simulated removal failure")
        original_remove(path)

    monkeypatch.setattr(git, "_run", fail_setup)
    monkeypatch.setattr(git, "_remove_worktree", fail_first_removal)

    with pytest.raises(QaWorktreeCleanupError) as captured:
        with worktree:
            raise AssertionError("partial worktree must not enter the body")

    assert failing_command in str(captured.value.body_error)
    assert "removal failure" in str(captured.value.cleanup_error)
    assert removal_attempts == 2
    assert not worktree_path.exists()
    assert str(worktree_path.resolve()) not in run_git(repo, "worktree", "list")


def test_partial_create_preserves_setup_and_prune_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune failure must retain the setup cause after removal succeeds."""

    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    worktree_path = repo / ".hoh" / "tmp" / "qa-prune"
    worktree = QaWorktree(git, worktree_path)
    original_run = git._run

    def fail_sparse(args, **kwargs):
        if args[0] == "sparse-checkout":
            raise GitError("simulated sparse-checkout failure")
        return original_run(args, **kwargs)

    def fail_prune() -> None:
        raise GitError("simulated prune failure")

    monkeypatch.setattr(git, "_run", fail_sparse)
    monkeypatch.setattr(git, "_prune_worktrees", fail_prune)

    with pytest.raises(QaWorktreeCleanupError) as captured:
        worktree.create(git.head_sha())

    assert "sparse-checkout failure" in str(captured.value.body_error)
    assert "prune failure" in str(captured.value.cleanup_error)
    assert not worktree_path.exists()
    assert str(worktree_path.resolve()) not in run_git(repo, "worktree", "list")
    worktree.remove()


def test_remove_refuses_a_path_outside_the_host_temp_root(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    outside = tmp_path / "not-host-owned"
    outside.mkdir()

    with pytest.raises(GitError, match=r"\.hoh[/\\]tmp"):
        QaWorktree(GitService(repo), outside).remove()

    assert outside.is_dir()


def test_remove_refuses_a_redirected_host_temp_root(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    host_state = repo / ".hoh"
    host_state.mkdir()
    redirected_root = host_state / "tmp"
    try:
        os.symlink(external, redirected_root, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {error}")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(redirected_root), str(external)],
            text=True,
            capture_output=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"directory redirects unavailable: {junction.stderr}")
    outside = external / "qa-redirected"
    outside.mkdir()

    with pytest.raises(GitError, match="redirected|inside the product"):
        QaWorktree(GitService(repo), redirected_root / "qa-redirected").remove()

    assert outside.is_dir()
