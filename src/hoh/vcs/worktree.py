"""Disposable detached QA worktrees bound to candidate commits."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from hoh.vcs.git import GitError, GitService


class QaWorktreeCleanupError(GitError):
    """Report cleanup failure without discarding an error from the QA body."""

    def __init__(self, body_error: BaseException, cleanup_error: GitError) -> None:
        super().__init__(
            f"QA worktree cleanup failed after {type(body_error).__name__}: {cleanup_error}"
        )
        self.body_error = body_error
        self.cleanup_error = cleanup_error


class QaWorktree:
    """Create and remove one detached worktree below ``.hoh/tmp``."""

    def __init__(self, git: GitService, path: Path) -> None:
        self._git = git
        self._path = Path(path)
        self._created = False

    def create(self, candidate_sha: str) -> Path:
        """Create a detached worktree fixed to *candidate_sha*."""

        path = self._validated_path()
        if self._created:
            raise GitError("QA worktree has already been created")
        candidate = self._git.rev_parse_in(
            self._git.repository, f"{candidate_sha}^{{commit}}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git._add_detached_worktree(path, candidate)
        self._created = True
        return path

    def remove(self) -> None:
        """Remove and prune a created worktree after validating its ownership boundary."""

        path = self._validated_path()
        if not self._created:
            return
        self._git._remove_worktree(path)
        self._created = False
        self._git._prune_worktrees()

    def __enter__(self) -> Path:
        return self.create(self._git.head_sha())

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        try:
            self.remove()
        except GitError as cleanup_error:
            if exception is not None:
                raise QaWorktreeCleanupError(exception, cleanup_error) from cleanup_error
            raise
        return False

    def _validated_path(self) -> Path:
        product = self._git.repository.resolve()
        allowed_root = (product / ".hoh" / "tmp").resolve()
        try:
            allowed_root.relative_to(product)
        except ValueError as error:
            raise GitError(
                "QA worktree root is redirected outside the product repository"
            ) from error
        path = (
            self._path.resolve()
            if self._path.is_absolute()
            else (product / self._path).resolve()
        )
        try:
            relative = path.relative_to(allowed_root)
        except ValueError as error:
            raise GitError(
                "QA worktree path must be under the product .hoh/tmp directory"
            ) from error
        if not relative.parts:
            raise GitError(
                "QA worktree path must be under the product .hoh/tmp directory"
            )
        return path
