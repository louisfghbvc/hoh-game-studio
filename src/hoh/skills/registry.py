from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

from hoh.models import Role


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class SkillValidationError(ValueError):
    """Raised when a skill module cannot be safely loaded or selected."""


class SkillConflictError(SkillValidationError):
    """Raised when a selected skill set contains incompatible modules."""


@dataclass(frozen=True)
class SkillDocument:
    skill_id: str
    version: str
    roles: tuple[Role, ...]
    adapters: tuple[str, ...]
    dependencies: tuple[str, ...]
    incompatible: tuple[str, ...]
    content: str
    sha256: str


@dataclass(frozen=True)
class SkillRegistry:
    _documents: Mapping[str, SkillDocument]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_documents", MappingProxyType(dict(self._documents)))

    @classmethod
    def load(cls, root: Path) -> SkillRegistry:
        if not root.is_dir():
            raise SkillValidationError(f"skill root does not exist: {root}")

        documents: dict[str, SkillDocument] = {}
        paths = sorted(root.rglob("*.md"), key=lambda path: path.relative_to(root).as_posix())
        for path in paths:
            document = _parse_document(path)
            if document.skill_id in documents:
                raise SkillValidationError(f"duplicate skill id: {document.skill_id}")
            documents[document.skill_id] = document

        for document in documents.values():
            for dependency in document.dependencies:
                if dependency not in documents:
                    raise SkillValidationError(
                        f"missing dependency {dependency!r} for skill {document.skill_id!r}"
                    )
        _validate_dependency_cycles(documents)
        return cls(documents)

    def select(
        self, role: Role, adapter: str, requested_ids: tuple[str, ...]
    ) -> tuple[SkillDocument, ...]:
        selected_ids: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def include(skill_id: str) -> None:
            document = self._documents.get(skill_id)
            if document is None:
                raise SkillValidationError(f"unknown skill id: {skill_id}")
            if skill_id in visited:
                return
            if skill_id in visiting:
                raise SkillValidationError(f"dependency cycle includes skill: {skill_id}")
            if role not in document.roles or not _adapter_matches(document.adapters, adapter):
                raise SkillValidationError(
                    f"skill {skill_id!r} is not applicable to role {role.value!r} and adapter {adapter!r}"
                )
            visiting.add(skill_id)
            for dependency in document.dependencies:
                include(dependency)
            visiting.remove(skill_id)
            visited.add(skill_id)
            selected_ids.append(skill_id)

        for skill_id in requested_ids:
            include(skill_id)

        selected = tuple(self._documents[skill_id] for skill_id in selected_ids)
        _validate_selection_conflicts(selected)
        return selected

    @staticmethod
    def render_bundle(skills: Iterable[SkillDocument]) -> str:
        return "\n\n".join(skill.content.strip() for skill in skills)


def _parse_document(path: Path) -> SkillDocument:
    raw_bytes = path.read_bytes()
    normalized_bytes = raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    try:
        source = normalized_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillValidationError(f"skill {path} is not UTF-8") from error

    metadata_text, content = _split_front_matter(source, path)
    try:
        metadata = tomllib.loads(metadata_text)
    except tomllib.TOMLDecodeError as error:
        raise SkillValidationError(f"invalid TOML front matter in {path}: {error}") from error
    if not isinstance(metadata, dict):
        raise SkillValidationError(f"invalid TOML front matter in {path}")

    skill_id = _required_string(metadata, "id", path)
    version = _required_string(metadata, "version", path)
    if not _SEMVER.fullmatch(version):
        raise SkillValidationError(f"invalid semantic version for skill {skill_id!r}: {version!r}")
    roles = _parse_roles(metadata, skill_id)
    adapters = _string_tuple(metadata, "adapters", skill_id, require_items=True)
    dependencies = _string_tuple(metadata, "dependencies", skill_id)
    incompatible = _string_tuple(metadata, "incompatible", skill_id)
    if not content.strip():
        raise SkillValidationError(f"skill {skill_id!r} has no instructions")

    return SkillDocument(
        skill_id=skill_id,
        version=version,
        roles=roles,
        adapters=adapters,
        dependencies=dependencies,
        incompatible=incompatible,
        content=content.strip(),
        sha256=hashlib.sha256(normalized_bytes).hexdigest(),
    )


def _split_front_matter(source: str, path: Path) -> tuple[str, str]:
    if not source.startswith("+++\n"):
        raise SkillValidationError(f"skill {path} must start with TOML front matter")
    closing = source.find("\n+++\n", len("+++\n"))
    if closing < 0:
        raise SkillValidationError(f"skill {path} has unterminated TOML front matter")
    return source[len("+++\n") : closing], source[closing + len("\n+++\n") :]


def _required_string(metadata: Mapping[str, object], key: str, path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise SkillValidationError(f"skill {path} requires a non-empty {key!r} string")
    return value


def _string_tuple(
    metadata: Mapping[str, object], key: str, skill_id: str, *, require_items: bool = False
) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SkillValidationError(f"skill {skill_id!r} requires {key!r} to be a string array")
    if require_items and not value:
        raise SkillValidationError(f"skill {skill_id!r} requires at least one {key!r} value")
    return tuple(value)


def _parse_roles(metadata: Mapping[str, object], skill_id: str) -> tuple[Role, ...]:
    names = _string_tuple(metadata, "roles", skill_id, require_items=True)
    roles: list[Role] = []
    for name in names:
        try:
            roles.append(Role(name))
        except ValueError as error:
            raise SkillValidationError(f"unknown role {name!r} for skill {skill_id!r}") from error
    return tuple(roles)


def _adapter_matches(adapters: tuple[str, ...], adapter: str) -> bool:
    return "*" in adapters or adapter in adapters


def _validate_dependency_cycles(documents: Mapping[str, SkillDocument]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in visiting:
            raise SkillValidationError(f"dependency cycle includes skill: {skill_id}")
        if skill_id in visited:
            return
        visiting.add(skill_id)
        for dependency in documents[skill_id].dependencies:
            visit(dependency)
        visiting.remove(skill_id)
        visited.add(skill_id)

    for skill_id in documents:
        visit(skill_id)


def _validate_selection_conflicts(skills: tuple[SkillDocument, ...]) -> None:
    selected_ids = {skill.skill_id for skill in skills}
    for skill in skills:
        for incompatible in skill.incompatible:
            if incompatible in selected_ids:
                raise SkillConflictError(
                    f"incompatible skills selected: {skill.skill_id} and {incompatible}"
                )
