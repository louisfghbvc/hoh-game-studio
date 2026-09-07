from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh.vcs.git import GitError, GitService
from hoh.vcs.worktree import QaWorktree


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


def test_remove_refuses_a_path_outside_the_host_temp_root(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    outside = tmp_path / "not-host-owned"
    outside.mkdir()

    with pytest.raises(GitError, match=r"\.hoh[/\\]tmp"):
        QaWorktree(GitService(repo), outside).remove()

    assert outside.is_dir()
