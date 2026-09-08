from __future__ import annotations

import os
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


def test_create_excludes_long_tracked_host_state_when_repo_disables_longpaths(
    tmp_path: Path,
) -> None:
    """QA needs the product tree, not recursively nested host evidence."""

    repo = initialized_repo(tmp_path)
    evidence_parent = repo / ".hoh" / "runs" / "run-a" / "loops" / "loop-0001"
    filename_length = 250 - len(str(evidence_parent)) - 1 - len(".json")
    assert filename_length > 0
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
