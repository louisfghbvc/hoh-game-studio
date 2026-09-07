"""Checked Git operations owned by the HoH host process."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath


class GitError(RuntimeError):
    """Raised when a Git operation or repository boundary is invalid."""


class DirtyWorktreeError(GitError):
    """Raised when an operation requires a clean product worktree."""


class ProtectedPathError(GitError):
    """Raised when a host-owned path changed across an agent invocation."""


@dataclass(frozen=True)
class PreparedCommit:
    """An exact commit object created without moving the checked-out branch."""

    kind: str
    loop_index: int
    parent_sha: str
    commit_sha: str
    tree_sha: str


@dataclass(frozen=True)
class _CandidateMutation:
    path: str
    entry_type: str
    sha256: str | None
    mode: str | None


class GitService:
    """Perform host-owned Git operations in one product repository."""

    _DEVELOPER_NAME = "hoh-developer[bot]"
    _DEVELOPER_EMAIL = "developer@hoh.local"
    _QA_NAME = "hoh-qa[bot]"
    _QA_EMAIL = "qa@hoh.local"

    def __init__(self, repository: Path) -> None:
        self._repository = Path(repository).resolve()

    @property
    def repository(self) -> Path:
        """Return the absolute product repository path."""

        return self._repository

    def assert_clean(self) -> None:
        """Reject tracked, staged, deleted, or untracked product changes."""

        status = self._run(("status", "--porcelain=v1", "--untracked-files=all"))
        if status:
            raise DirtyWorktreeError(f"product worktree is dirty: {status.strip()}")

    def head_sha(self) -> str:
        """Return the commit currently checked out by the product worktree."""

        return self._run(("rev-parse", "--verify", "HEAD")).strip()

    def create_run_branch(self, run_id: str) -> str:
        """Create and switch to the dedicated branch for *run_id*."""

        self._validate_run_id(run_id)
        self.assert_clean()
        branch = f"hoh/run-{run_id}"
        self._run(("switch", "-c", branch))
        return branch

    def changed_paths(self, base_sha: str) -> tuple[str, ...]:
        """Return repository-relative changes from *base_sha*, including untracked files."""

        tracked = self._split_nul(
            self._run(("diff", "--name-only", "--relative", "-z", base_sha, "--"))
        )
        untracked = self._split_nul(
            self._run(("ls-files", "--others", "--exclude-standard", "-z", "--"))
        )
        return tuple(sorted(set(tracked) | set(untracked)))

    def candidate_mutation_manifest(
        self, base_sha: str, paths: tuple[str, ...]
    ) -> dict[str, object]:
        """Describe the exact Git entries produced by staging product mutations."""

        if self.rev_parse_in(self._repository, f"{base_sha}^{{commit}}") != base_sha:
            raise GitError("candidate mutation base must be a full commit identity")
        selected_paths = self._candidate_paths(paths)
        current_paths = tuple(
            path
            for path in self.changed_paths(base_sha)
            if path != ".hoh" and not path.startswith(".hoh/")
        )
        if current_paths != selected_paths:
            raise GitError(
                "candidate mutation paths do not match the literal manifest path set"
            )
        pathspecs = tuple(f":(top,literal){path}" for path in selected_paths)
        index_path = self._temporary_index_path()
        environment = {"GIT_INDEX_FILE": str(index_path)}
        try:
            self._run(("read-tree", base_sha), env=environment)
            self._run(("add", "--all", "--", *pathspecs), env=environment)
            mutations = self._staged_candidate_mutations(
                base_sha, environment, selected_paths
            )
        finally:
            index_path.unlink(missing_ok=True)
            index_path.with_name(index_path.name + ".lock").unlink(missing_ok=True)
        return {
            "schema_version": 1,
            "base_sha": base_sha,
            "entries": [
                {
                    "path": mutation.path,
                    "type": mutation.entry_type,
                    "sha256": mutation.sha256,
                    "mode": mutation.mode,
                }
                for mutation in mutations
            ],
        }

    def snapshot_paths(self, paths: tuple[str, ...]) -> dict[str, str]:
        """Hash each protected path recursively, including absent paths and empty directories."""

        snapshot: dict[str, str] = {}
        for supplied_path in paths:
            relative_path, absolute_path = self._protected_path(supplied_path)
            snapshot[relative_path] = self._recursive_digest(absolute_path)
        return snapshot

    def assert_snapshot_unchanged(self, snapshot: Mapping[str, str]) -> None:
        """Reject any protected root whose recursive digest has changed."""

        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a mapping")
        current = self.snapshot_paths(tuple(snapshot))
        changed = sorted(
            path
            for path in set(snapshot) | set(current)
            if snapshot.get(path) != current.get(path)
        )
        if changed:
            raise ProtectedPathError(
                "protected path changed: " + ", ".join(changed)
            )

    def commit_candidate(self, loop_index: int, summary: str) -> str:
        """Commit all production changes while excluding host-owned ``.hoh`` state."""

        return self.land_prepared_commit(
            self.prepare_candidate(loop_index, summary)
        )

    def prepare_candidate(
        self,
        loop_index: int,
        summary: str,
        paths: tuple[str, ...] | None = None,
        *,
        mutation_manifest: Mapping[str, object] | None = None,
    ) -> PreparedCommit:
        """Create the exact candidate commit object without moving the run branch."""

        self._require_run_branch()
        message = f"feat(loop-{self._loop_number(loop_index)}): {self._summary(summary)}"
        staged_host_state = self._split_nul(
            self._run(("diff", "--cached", "--name-only", "-z", "--", ".hoh"))
        )
        if staged_host_state:
            raise ProtectedPathError(
                "protected path is staged: " + ", ".join(staged_host_state)
            )
        parent_sha = self.head_sha()
        expected_mutations = (
            self._candidate_mutations(mutation_manifest, parent_sha)
            if mutation_manifest is not None
            else None
        )
        current_paths = tuple(
            path
            for path in self.changed_paths(parent_sha)
            if path != ".hoh" and not path.startswith(".hoh/")
        )
        selected_paths = self._candidate_paths(
            (
                tuple(mutation.path for mutation in expected_mutations)
                if paths is None and expected_mutations is not None
                else current_paths if paths is None else paths
            )
        )
        if expected_mutations is not None and selected_paths != tuple(
            mutation.path for mutation in expected_mutations
        ):
            raise GitError(
                "candidate mutation manifest does not match the literal path set"
            )
        if current_paths != selected_paths:
            raise GitError(
                "candidate mutation paths do not match the validated literal path set"
            )
        production_pathspec = tuple(
            f":(top,literal){path}" for path in selected_paths
        )
        return self._prepare_commit(
            "candidate",
            loop_index,
            message,
            self._DEVELOPER_NAME,
            self._DEVELOPER_EMAIL,
            production_pathspec,
            staged_validator=(
                None
                if expected_mutations is None
                else lambda staged_parent, environment: self._assert_staged_candidate(
                    parent_sha,
                    staged_parent,
                    environment,
                    expected_mutations,
                )
            ),
        )

    def commit_evidence(self, loop_index: int, paths: tuple[Path, ...]) -> str:
        """Commit only host-selected state and evidence paths."""

        return self.land_prepared_commit(
            self.prepare_evidence(loop_index, paths)
        )

    def prepare_evidence(
        self, loop_index: int, paths: tuple[Path, ...]
    ) -> PreparedCommit:
        """Create an exact selected-evidence commit without moving the run branch."""

        self._require_run_branch()
        selected_paths = self._selected_paths(paths)
        message = (
            f"test(loop-{self._loop_number(loop_index)}): record candidate evidence"
        )
        return self._prepare_commit(
            "evidence",
            loop_index,
            message,
            self._QA_NAME,
            self._QA_EMAIL,
            selected_paths,
        )

    def land_prepared_commit(self, prepared: PreparedCommit) -> str:
        """Move the run ref to exactly *prepared*, using its parent as a CAS guard."""

        if not isinstance(prepared, PreparedCommit):
            raise TypeError("prepared must be a PreparedCommit")
        if prepared.kind not in {"candidate", "evidence"}:
            raise GitError("prepared commit kind is invalid")
        if prepared.loop_index < 1:
            raise GitError("prepared commit loop index is invalid")
        self._require_run_branch()
        try:
            resolved = self.rev_parse_in(self._repository, f"{prepared.commit_sha}^{{commit}}")
            parent = self.rev_parse_in(self._repository, f"{prepared.commit_sha}^")
            tree = self.rev_parse_in(self._repository, f"{prepared.commit_sha}^{{tree}}")
        except GitError as error:
            raise GitError("prepared commit object cannot be resolved") from error
        if (
            resolved != prepared.commit_sha
            or parent != prepared.parent_sha
            or tree != prepared.tree_sha
        ):
            raise GitError("prepared commit identity does not match its object")

        head = self.head_sha()
        if head == prepared.parent_sha:
            self._run(
                (
                    "update-ref",
                    "HEAD",
                    prepared.commit_sha,
                    prepared.parent_sha,
                )
            )
        elif head != prepared.commit_sha:
            raise GitError(
                "Git HEAD is neither the prepared commit nor its expected parent"
            )

        # Preparation uses an isolated index.  Reconcile the real index after
        # the compare-and-swap; repeating this also repairs a crash immediately
        # after update-ref without touching working-tree files.
        self._run(("reset", "--mixed", prepared.commit_sha))
        if self.head_sha() != prepared.commit_sha:
            raise GitError("Git HEAD did not land on the prepared commit")
        return prepared.commit_sha

    def rev_parse_in(self, worktree: Path, revision: str) -> str:
        """Resolve *revision* from a specific worktree."""

        return self._run(
            ("rev-parse", "--verify", revision), cwd=Path(worktree).resolve()
        ).strip()

    def current_branch_in(self, worktree: Path) -> str:
        """Return the checked-out branch, or an empty string for detached HEAD."""

        return self._run(
            ("branch", "--show-current"), cwd=Path(worktree).resolve()
        ).strip()

    def _add_detached_worktree(self, path: Path, candidate_sha: str) -> None:
        self._run(("worktree", "add", "--detach", str(path), candidate_sha))

    def _remove_worktree(self, path: Path) -> None:
        self._run(("worktree", "remove", "--force", str(path)))

    def _prune_worktrees(self) -> None:
        self._run(("worktree", "prune"))

    def _prepare_commit(
        self,
        kind: str,
        loop_index: int,
        message: str,
        author_name: str,
        author_email: str,
        paths: Sequence[str],
        staged_validator: Callable[[str, Mapping[str, str]], None] | None = None,
    ) -> PreparedCommit:
        parent_sha = self.head_sha()
        parent_tree = self.rev_parse_in(self._repository, f"{parent_sha}^{{tree}}")
        index_path = self._temporary_index_path()
        environment = {
            "GIT_INDEX_FILE": str(index_path),
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        try:
            self._run(("read-tree", parent_sha), env=environment)
            self._run(("add", "--all", "--", *paths), env=environment)
            if staged_validator is not None:
                staged_validator(parent_sha, environment)
            tree_sha = self._run(("write-tree",), env=environment).strip()
            if tree_sha == parent_tree:
                raise GitError(f"prepared {kind} commit has no changes")
            commit_sha = self._run(
                ("commit-tree", tree_sha, "-p", parent_sha, "-m", message),
                env=environment,
            ).strip()
        finally:
            index_path.unlink(missing_ok=True)
            index_path.with_name(index_path.name + ".lock").unlink(missing_ok=True)
        return PreparedCommit(
            kind=kind,
            loop_index=loop_index,
            parent_sha=parent_sha,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
        )

    def _temporary_index_path(self) -> Path:
        supplied = self._run(("rev-parse", "--git-path", "index")).strip()
        real_index = Path(supplied)
        if not real_index.is_absolute():
            real_index = self._repository / real_index
        directory = real_index.resolve().parent
        descriptor, temporary = tempfile.mkstemp(
            dir=directory, prefix="hoh-index-", suffix=".tmp"
        )
        os.close(descriptor)
        path = Path(temporary)
        path.unlink()
        return path

    def _require_run_branch(self) -> None:
        branch = self.current_branch_in(self._repository)
        if not branch.startswith("hoh/run-"):
            raise GitError(
                "candidate and evidence commits require a hoh/run- branch"
            )

    def _selected_paths(self, paths: tuple[Path, ...]) -> tuple[str, ...]:
        if not paths:
            raise GitError("at least one evidence path is required")
        selected: list[str] = []
        for supplied_path in paths:
            path = Path(supplied_path)
            absolute_path = (
                path.resolve()
                if path.is_absolute()
                else (self._repository / path).resolve()
            )
            try:
                relative_path = absolute_path.relative_to(self._repository)
            except ValueError as error:
                raise GitError("evidence paths must be inside the product repository") from error
            if not relative_path.parts or relative_path.parts[0] == ".git":
                raise GitError("evidence paths must not select the repository or .git")
            selected.append(f":(top,literal){relative_path.as_posix()}")
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _candidate_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
        if not paths:
            raise GitError("at least one candidate mutation path is required")
        selected: list[str] = []
        for supplied in paths:
            if not isinstance(supplied, str) or not supplied:
                raise GitError("candidate mutation paths must be non-empty strings")
            path = PurePosixPath(supplied)
            if (
                path.is_absolute()
                or supplied != path.as_posix()
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[0] in {".git", ".hoh"}
            ):
                raise GitError("candidate mutation path is not a normalized product path")
            selected.append(supplied)
        if len(selected) != len(set(selected)):
            raise GitError("candidate mutation path set contains duplicates")
        return tuple(sorted(selected))

    def _candidate_mutations(
        self, manifest: Mapping[str, object], parent_sha: str
    ) -> tuple[_CandidateMutation, ...]:
        if (
            not isinstance(manifest, Mapping)
            or set(manifest) != {"schema_version", "base_sha", "entries"}
            or isinstance(manifest.get("schema_version"), bool)
            or manifest.get("schema_version") != 1
            or manifest.get("base_sha") != parent_sha
        ):
            raise GitError("candidate mutation manifest is malformed or has a wrong parent")
        raw_entries = manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise GitError("candidate mutation manifest entries are malformed")
        mutations: list[_CandidateMutation] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
                "path",
                "type",
                "sha256",
                "mode",
            }:
                raise GitError("candidate mutation manifest entry is malformed")
            path = raw_entry.get("path")
            entry_type = raw_entry.get("type")
            digest = raw_entry.get("sha256")
            mode = raw_entry.get("mode")
            if not isinstance(path, str):
                raise GitError("candidate mutation manifest path is malformed")
            self._candidate_paths((path,))
            if entry_type == "deleted":
                if digest is not None or mode is not None:
                    raise GitError(
                        "deleted candidate mutation must not have an index entry"
                    )
            elif entry_type == "file":
                if not self._is_sha256(digest):
                    raise GitError("candidate mutation content hash is malformed")
                if not isinstance(mode, str) or mode not in {"100644", "100755"}:
                    raise GitError("candidate file mode is malformed")
            elif entry_type == "symlink":
                if not self._is_sha256(digest):
                    raise GitError("candidate mutation content hash is malformed")
                if mode != "120000":
                    raise GitError("candidate symlink mode is malformed")
            else:
                raise GitError("candidate mutation type is malformed")
            mutations.append(
                _CandidateMutation(path, str(entry_type), digest, mode)
            )
        paths = tuple(mutation.path for mutation in mutations)
        if paths != self._candidate_paths(paths):
            raise GitError("candidate mutation manifest path set is ambiguous")
        return tuple(mutations)

    def _assert_staged_candidate(
        self,
        expected_parent_sha: str,
        parent_sha: str,
        environment: Mapping[str, str],
        mutations: tuple[_CandidateMutation, ...],
    ) -> None:
        if parent_sha != expected_parent_sha:
            raise GitError(
                "staged candidate parent does not match the durable mutation manifest"
            )
        staged = self._staged_candidate_mutations(
            parent_sha,
            environment,
            tuple(mutation.path for mutation in mutations),
        )
        if staged != mutations:
            raise GitError(
                "staged candidate entries do not match the durable mutation manifest"
            )

    def _staged_candidate_mutations(
        self,
        parent_sha: str,
        environment: Mapping[str, str],
        expected_paths: tuple[str, ...],
    ) -> tuple[_CandidateMutation, ...]:
        staged_paths = tuple(
            sorted(
                set(
                    self._split_nul(
                        self._run(
                            (
                                "diff",
                                "--cached",
                                "--name-only",
                                "--relative",
                                "-z",
                                parent_sha,
                                "--",
                            ),
                            env=environment,
                        )
                    )
                )
            )
        )
        if staged_paths != expected_paths:
            raise GitError(
                "staged candidate paths do not match the durable mutation manifest"
            )
        mutations: list[_CandidateMutation] = []
        for path in expected_paths:
            pathspec = f":(top,literal){path}"
            records = self._split_nul(
                self._run(("ls-files", "--stage", "-z", "--", pathspec), env=environment)
            )
            if not records:
                mutations.append(_CandidateMutation(path, "deleted", None, None))
                continue
            if len(records) != 1 or "\t" not in records[0]:
                raise GitError("staged candidate entry is missing or ambiguous")
            metadata, staged_path = records[0].split("\t", 1)
            fields = metadata.split()
            if len(fields) != 3 or fields[2] != "0" or staged_path != path:
                raise GitError("staged candidate entry identity is malformed")
            mode, object_id, _ = fields
            staged_type = (
                "file"
                if mode in {"100644", "100755"}
                else "symlink" if mode == "120000" else "unsupported"
            )
            if staged_type == "unsupported":
                raise GitError(
                    "staged candidate entry type is unsupported"
                )
            digest = hashlib.sha256(
                self._run_bytes(("cat-file", "blob", object_id), env=environment)
            ).hexdigest()
            mutations.append(_CandidateMutation(path, staged_type, digest, mode))
        return tuple(mutations)

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _protected_path(self, supplied_path: str) -> tuple[str, Path]:
        path = Path(supplied_path)
        absolute_path = (
            path.resolve()
            if path.is_absolute()
            else (self._repository / path).resolve()
        )
        try:
            relative_path = absolute_path.relative_to(self._repository)
        except ValueError as error:
            raise ProtectedPathError(
                "protected paths must be inside the product repository"
            ) from error
        if not relative_path.parts:
            raise ProtectedPathError("the repository root cannot be a protected path")
        return relative_path.as_posix(), absolute_path

    @staticmethod
    def _recursive_digest(path: Path) -> str:
        digest = hashlib.sha256()

        def add_record(*fields: bytes) -> None:
            for field in fields:
                digest.update(len(field).to_bytes(8, byteorder="big"))
                digest.update(field)

        def add(entry: Path, relative_name: str) -> None:
            encoded_name = relative_name.encode("utf-8", errors="surrogateescape")
            if entry.is_symlink():
                target = os.readlink(entry).encode("utf-8", errors="surrogateescape")
                add_record(b"link", encoded_name, target)
                return
            if not entry.exists():
                add_record(b"missing", encoded_name)
                return
            if entry.is_dir():
                add_record(b"directory-start", encoded_name)
                for child in sorted(entry.iterdir(), key=lambda item: item.name):
                    child_name = f"{relative_name}/{child.name}" if relative_name else child.name
                    add(child, child_name)
                add_record(b"directory-end", encoded_name)
                return
            content_digest = hashlib.sha256()
            with entry.open("rb") as protected_file:
                for chunk in iter(lambda: protected_file.read(1024 * 1024), b""):
                    content_digest.update(chunk)
            add_record(b"file", encoded_name, content_digest.digest())

        add(path, "")
        return digest.hexdigest()

    @staticmethod
    def _split_nul(output: str) -> tuple[str, ...]:
        return tuple(path for path in output.split("\0") if path)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if (
            not run_id
            or run_id in {".", ".."}
            or Path(run_id).name != run_id
            or "/" in run_id
            or "\\" in run_id
        ):
            raise ValueError("run_id must be a non-empty single path component")

    @staticmethod
    def _loop_number(loop_index: int) -> str:
        if loop_index < 1:
            raise ValueError("loop_index must be positive")
        return f"{loop_index:04d}"

    @staticmethod
    def _summary(summary: str) -> str:
        normalized = summary.strip()
        if not normalized or "\n" in normalized or "\r" in normalized:
            raise ValueError("summary must be a non-empty single line")
        return normalized

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        working_directory = self._repository if cwd is None else Path(cwd)
        environment = None
        if env is not None:
            environment = os.environ.copy()
            environment.update(env)
        completed = subprocess.run(
            ["git", *args],
            cwd=working_directory,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            command_name = self._command_name(args)
            detail = completed.stderr.strip() or completed.stdout.strip()
            detail = self._sanitize(detail, working_directory)
            if not detail:
                detail = f"exit status {completed.returncode}"
            raise GitError(f"git {command_name} failed: {detail}")
        return completed.stdout

    def _run_bytes(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> bytes:
        working_directory = self._repository if cwd is None else Path(cwd)
        environment = None
        if env is not None:
            environment = os.environ.copy()
            environment.update(env)
        completed = subprocess.run(
            ["git", *args],
            cwd=working_directory,
            capture_output=True,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            command_name = self._command_name(args)
            detail = (completed.stderr or completed.stdout).decode(
                errors="replace"
            ).strip()
            detail = self._sanitize(detail, working_directory)
            if not detail:
                detail = f"exit status {completed.returncode}"
            raise GitError(f"git {command_name} failed: {detail}")
        return completed.stdout

    def _sanitize(self, value: str, cwd: Path) -> str:
        sanitized = value
        for path in {self._repository, cwd.resolve()}:
            sanitized = sanitized.replace(str(path), "<repo>")
            sanitized = sanitized.replace(path.as_posix(), "<repo>")
        return sanitized

    @staticmethod
    def _command_name(args: Sequence[str]) -> str:
        index = 0
        while index < len(args) and args[index] == "-c":
            index += 2
        return args[index] if index < len(args) else "command"
