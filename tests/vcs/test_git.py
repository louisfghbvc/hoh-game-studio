from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh.vcs.git import DirtyWorktreeError, GitError, GitService, ProtectedPathError


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


def test_assert_clean_rejects_untracked_product_content(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    (repo / "untracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(DirtyWorktreeError, match="untracked.txt"):
        GitService(repo).assert_clean()


def test_create_run_branch_uses_the_dedicated_namespace(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)

    branch = GitService(repo).create_run_branch("run-abc")

    assert branch == "hoh/run-run-abc"
    assert run_git(repo, "branch", "--show-current") == branch


def test_candidate_commit_excludes_host_state_and_uses_host_author(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    (repo / "product.txt").write_text("changed", encoding="utf-8")
    (repo / ".hoh").mkdir()
    (repo / ".hoh" / "owned.json").write_text("{}", encoding="utf-8")

    candidate = git.commit_candidate(1, "change product")

    assert candidate == run_git(repo, "rev-parse", "HEAD")
    assert run_git(repo, "show", "-s", "--format=%s", "HEAD") == (
        "feat(loop-0001): change product"
    )
    assert run_git(repo, "show", "-s", "--format=%an <%ae>", "HEAD") == (
        "hoh-developer[bot] <developer@hoh.local>"
    )
    assert run_git(repo, "show", "HEAD:product.txt") == "changed"
    assert run_git(repo, "ls-tree", "-r", "--name-only", "HEAD") == "product.txt"
    assert (repo / ".hoh" / "owned.json").is_file()


def test_candidate_commit_is_rejected_on_the_default_branch(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    (repo / "product.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(GitError, match=r"hoh/run-"):
        GitService(repo).commit_candidate(1, "must not land on main")


def test_changed_paths_includes_modified_deleted_and_untracked_files(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    (repo / "deleted.txt").write_text("tracked", encoding="utf-8")
    run_git(repo, "add", "deleted.txt")
    run_git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "add second file",
    )
    base_sha = run_git(repo, "rev-parse", "HEAD")
    (repo / "product.txt").write_text("modified", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    (repo / "new.txt").write_text("untracked", encoding="utf-8")

    assert GitService(repo).changed_paths(base_sha) == (
        "deleted.txt",
        "new.txt",
        "product.txt",
    )


def test_protected_change_is_rejected(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    (repo / ".hoh").mkdir()
    (repo / ".hoh" / "owned.json").write_text("{}", encoding="utf-8")
    snapshot = git.snapshot_paths((".hoh", ".git"))
    (repo / ".hoh" / "owned.json").write_text(
        '{"changed": true}', encoding="utf-8"
    )

    with pytest.raises(ProtectedPathError, match=r"\.hoh"):
        git.assert_snapshot_unchanged(snapshot)


def test_protected_snapshot_detects_created_and_deleted_entries(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    protected = repo / "protected"
    protected.mkdir()
    (protected / "deleted.txt").write_text("tracked", encoding="utf-8")
    snapshot = git.snapshot_paths(("protected", "missing"))
    (protected / "deleted.txt").unlink()
    (repo / "missing").mkdir()
    (repo / "missing" / "created.txt").write_text("new", encoding="utf-8")

    with pytest.raises(ProtectedPathError, match="missing|protected"):
        git.assert_snapshot_unchanged(snapshot)


def test_evidence_commit_contains_only_selected_host_paths(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    state = repo / ".hoh" / "state.json"
    evidence = repo / ".hoh" / "evidence" / "result.json"
    unselected = repo / ".hoh" / "private.tmp"
    evidence.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    evidence.write_text('{"status": "pass"}', encoding="utf-8")
    unselected.write_text("do not stage", encoding="utf-8")

    commit = git.commit_evidence(2, (state, evidence))

    assert commit == run_git(repo, "rev-parse", "HEAD")
    assert run_git(repo, "show", "-s", "--format=%s", "HEAD") == (
        "test(loop-0002): record candidate evidence"
    )
    assert run_git(repo, "show", "-s", "--format=%an <%ae>", "HEAD") == (
        "hoh-qa[bot] <qa@hoh.local>"
    )
    assert run_git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        ".hoh/evidence/result.json",
        ".hoh/state.json",
        "product.txt",
    ]
    assert ".hoh/private.tmp" in run_git(repo, "status", "--porcelain")


def test_git_error_names_failed_command_without_exposing_repository_path(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)

    with pytest.raises(GitError) as raised:
        GitService(repo).rev_parse_in(repo, "does-not-exist")

    assert "rev-parse" in str(raised.value)
    assert str(repo) not in str(raised.value)
