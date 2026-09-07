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


def test_prepared_candidate_is_exact_and_does_not_move_head_until_landed(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    parent = git.head_sha()
    (repo / "product.txt").write_text("changed", encoding="utf-8")
    (repo / ".hoh").mkdir()
    (repo / ".hoh" / "scratch.json").write_text("{}", encoding="utf-8")

    prepared = git.prepare_candidate(1, "change product")

    assert git.head_sha() == parent
    assert prepared.parent_sha == parent
    assert prepared.commit_sha != parent
    assert run_git(repo, "show", "-s", "--format=%s", prepared.commit_sha) == (
        "feat(loop-0001): change product"
    )
    assert run_git(repo, "show", "-s", "--format=%an <%ae>", prepared.commit_sha) == (
        "hoh-developer[bot] <developer@hoh.local>"
    )
    assert run_git(repo, "ls-tree", "-r", "--name-only", prepared.commit_sha) == (
        "product.txt"
    )

    assert git.land_prepared_commit(prepared) == prepared.commit_sha
    assert git.head_sha() == prepared.commit_sha
    assert "product.txt" not in run_git(repo, "status", "--porcelain")


def test_landing_prepared_candidate_rejects_an_unexpected_direct_child(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    (repo / "product.txt").write_text("intended", encoding="utf-8")
    prepared = git.prepare_candidate(1, "intended candidate")
    run_git(
        repo,
        "-c",
        "user.name=external",
        "-c",
        "user.email=external@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "unexpected direct child",
    )
    external = git.head_sha()

    with pytest.raises(GitError, match="prepared|HEAD|parent"):
        git.land_prepared_commit(prepared)

    assert git.head_sha() == external
    assert (repo / "product.txt").read_text(encoding="utf-8") == "intended"


def test_prepare_candidate_rejects_repository_mutations_outside_literal_set(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    (repo / "product.txt").write_text("intended", encoding="utf-8")
    (repo / "injected.txt").write_text("must not be committed", encoding="utf-8")

    with pytest.raises(GitError, match="mutation|path|candidate"):
        git.prepare_candidate(1, "bounded candidate", ("product.txt",))


def test_prepare_candidate_handles_deletion_and_literal_special_character_path(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    special = repo / "feature[1].txt"
    deleted = repo / "delete-me.txt"
    deleted.write_text("remove me", encoding="utf-8")
    run_git(repo, "add", "delete-me.txt")
    run_git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "add deletion fixture",
    )
    git = GitService(repo)
    git.create_run_branch("run-abc")
    deleted.unlink()
    special.write_text("literal path", encoding="utf-8")

    prepared = git.prepare_candidate(
        1,
        "literal candidate",
        ("delete-me.txt", "feature[1].txt"),
    )
    git.land_prepared_commit(prepared)

    committed = run_git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "delete-me.txt" not in committed
    assert "feature[1].txt" in committed
    assert run_git(repo, "show", "HEAD:feature[1].txt") == "literal path"


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


def test_protected_snapshot_records_cannot_be_absorbed_into_file_content(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    protected = repo / "protected"
    protected.mkdir()
    (protected / "a").write_bytes(b"A")
    (protected / "b").write_bytes(b"B")
    snapshot = git.snapshot_paths(("protected",))

    (protected / "a").write_bytes(b"Afile\0b\0B")
    (protected / "b").unlink()

    with pytest.raises(ProtectedPathError, match="protected"):
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


def test_prepared_evidence_object_contains_only_the_exact_selected_paths(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    selected = repo / ".hoh" / "evidence.json"
    raw_response = repo / ".hoh" / "responses" / "raw.json"
    selected.parent.mkdir(parents=True)
    raw_response.parent.mkdir(parents=True)
    selected.write_text('{"status": "pass"}', encoding="utf-8")
    raw_response.write_text('{"untrusted": true}', encoding="utf-8")

    prepared = git.prepare_evidence(1, (selected,))

    assert git.head_sha() == prepared.parent_sha
    committed = run_git(
        repo, "ls-tree", "-r", "--name-only", prepared.commit_sha
    ).splitlines()
    assert ".hoh/evidence.json" in committed
    assert ".hoh/responses/raw.json" not in committed

    git.land_prepared_commit(prepared)
    assert ".hoh/responses/raw.json" in run_git(
        repo, "status", "--porcelain", "--untracked-files=all"
    )


def test_evidence_path_with_pathspec_metacharacters_is_literal(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git = GitService(repo)
    git.create_run_branch("run-abc")
    evidence = repo / ".hoh" / "evidence"
    evidence.mkdir(parents=True)
    selected = evidence / "result[0].json"
    wildcard_match = evidence / "result0.json"
    selected.write_text("selected", encoding="utf-8")
    wildcard_match.write_text("must remain unselected", encoding="utf-8")

    git.commit_evidence(1, (selected,))

    committed = run_git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".hoh/evidence/result[0].json" in committed
    assert ".hoh/evidence/result0.json" not in committed
    assert ".hoh/evidence/result0.json" in run_git(repo, "status", "--porcelain")


def test_git_error_names_failed_command_without_exposing_repository_path(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)

    with pytest.raises(GitError) as raised:
        GitService(repo).rev_parse_in(repo, "does-not-exist")

    assert "rev-parse" in str(raised.value)
    assert str(repo) not in str(raised.value)
