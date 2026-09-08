from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hoh.models import Role
from hoh.skills.registry import (
    SkillConflictError,
    SkillRegistry,
    SkillValidationError,
)


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    return tmp_path / "skills"


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    version: str = "1.0.0",
    roles: tuple[str, ...] = ("qa",),
    adapters: tuple[str, ...] = ("*",),
    dependencies: tuple[str, ...] = (),
    incompatible: tuple[str, ...] = (),
    body: str = "# Skill\n\nFollow these instructions.\n",
) -> Path:
    path = root / f"{skill_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = "\n".join(
        (
            "+++",
            f'id = "{skill_id}"',
            f'version = "{version}"',
            "roles = [" + ", ".join(f'"{role}"' for role in roles) + "]",
            "adapters = [" + ", ".join(f'"{adapter}"' for adapter in adapters) + "]",
            "dependencies = ["
            + ", ".join(f'"{dependency}"' for dependency in dependencies)
            + "]",
            "incompatible = ["
            + ", ".join(f'"{skill}"' for skill in incompatible)
            + "]",
            "+++",
            "",
            body,
        )
    )
    path.write_text(document, encoding="utf-8", newline="\n")
    return path


def test_select_returns_only_matching_skills_in_requested_order(skill_root: Path) -> None:
    _write_skill(skill_root, "core.planning", roles=("planner",))
    _write_skill(skill_root, "godot.runtime-testing", adapters=("godot",))
    _write_skill(skill_root, "core.evidence-grounded-qa")

    registry = SkillRegistry.load(skill_root)

    selected = registry.select(
        Role.QA,
        "godot",
        ("core.evidence-grounded-qa", "godot.runtime-testing"),
    )

    assert [skill.skill_id for skill in selected] == [
        "core.evidence-grounded-qa",
        "godot.runtime-testing",
    ]
    assert all(len(skill.sha256) == 64 for skill in selected)


def test_select_includes_dependencies_before_the_requested_skill(skill_root: Path) -> None:
    _write_skill(skill_root, "core.foundation")
    _write_skill(
        skill_root,
        "godot.runtime-testing",
        adapters=("godot",),
        dependencies=("core.foundation",),
    )

    registry = SkillRegistry.load(skill_root)

    selected = registry.select(Role.QA, "godot", ("godot.runtime-testing",))

    assert [skill.skill_id for skill in selected] == [
        "core.foundation",
        "godot.runtime-testing",
    ]


def test_select_rejects_incompatible_skills(skill_root: Path) -> None:
    _write_skill(skill_root, "fixture.a", incompatible=("fixture.b",))
    _write_skill(skill_root, "fixture.b")
    registry = SkillRegistry.load(skill_root)

    with pytest.raises(SkillConflictError, match="fixture.a.*fixture.b"):
        registry.select(Role.QA, "godot", ("fixture.a", "fixture.b"))


def test_select_rejects_a_skill_outside_its_role_or_adapter(skill_root: Path) -> None:
    _write_skill(skill_root, "godot.planner", roles=("planner",), adapters=("godot",))
    registry = SkillRegistry.load(skill_root)

    with pytest.raises(SkillValidationError, match="not applicable"):
        registry.select(Role.QA, "godot", ("godot.planner",))

    with pytest.raises(SkillValidationError, match="not applicable"):
        registry.select(Role.PLANNER, "command", ("godot.planner",))


def test_load_normalizes_complete_file_bytes_before_hashing(skill_root: Path) -> None:
    path = _write_skill(skill_root, "core.hashable")
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    expected = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    registry = SkillRegistry.load(skill_root)

    assert registry.select(Role.QA, "godot", ("core.hashable",))[0].sha256 == expected


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    (
        ("duplicate", "duplicate", "duplicate skill id"),
        ("bad-version", "1.0", "semantic version"),
    ),
)
def test_load_rejects_duplicate_ids_and_invalid_versions(
    skill_root: Path, first: str, second: str, expected: str
) -> None:
    if first == "duplicate":
        _write_skill(skill_root / "one", first)
        _write_skill(skill_root / "two", second)
    else:
        _write_skill(skill_root, first, version=second)

    with pytest.raises(SkillValidationError, match=expected):
        SkillRegistry.load(skill_root)


def test_load_rejects_unknown_roles_missing_dependencies_and_dependency_cycles(
    skill_root: Path,
) -> None:
    _write_skill(skill_root / "unknown", "unknown", roles=("reviewer",))

    with pytest.raises(SkillValidationError, match="unknown role"):
        SkillRegistry.load(skill_root / "unknown")

    _write_skill(skill_root / "missing", "missing", dependencies=("absent",))

    with pytest.raises(SkillValidationError, match="missing dependency"):
        SkillRegistry.load(skill_root / "missing")

    _write_skill(skill_root / "cycle", "a", dependencies=("b",))
    _write_skill(skill_root / "cycle", "b", dependencies=("a",))

    with pytest.raises(SkillValidationError, match="dependency cycle"):
        SkillRegistry.load(skill_root / "cycle")


def test_load_rejects_a_skill_without_instruction_content(skill_root: Path) -> None:
    _write_skill(skill_root, "empty", body="\n")

    with pytest.raises(SkillValidationError, match="instructions"):
        SkillRegistry.load(skill_root)


def test_render_bundle_preserves_the_selected_order(skill_root: Path) -> None:
    _write_skill(skill_root, "first", body="# First\n\nFirst instruction.\n")
    _write_skill(skill_root, "second", body="# Second\n\nSecond instruction.\n")
    registry = SkillRegistry.load(skill_root)
    selected = registry.select(Role.QA, "godot", ("second", "first"))

    rendered = registry.render_bundle(selected)

    assert rendered.index("# Second") < rendered.index("# First")
    assert "Second instruction." in rendered
    assert "First instruction." in rendered
