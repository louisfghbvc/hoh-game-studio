from __future__ import annotations

import pytest

from hoh.models import Role
from hoh.prompts import PromptRenderer, PromptRenderingError
from hoh.skills.registry import SkillDocument


def skill(identifier: str, content: str) -> SkillDocument:
    return SkillDocument(
        skill_id=identifier,
        version="1.2.3",
        roles=(Role.PLANNER,),
        adapters=("*",),
        dependencies=(),
        incompatible=(),
        content=content,
        sha256="a" * 64,
    )


def context() -> dict[str, object]:
    return {
        "PUBLIC_PRD": "# Public PRD\n\nThe product must boot.",
        "PROJECT_SUMMARY": {"files": ["game/main.tscn"], "language": "GDScript"},
        "PREVIOUS_EVIDENCE": [],
        "ISSUE_LEDGER": [{"id": "gap-boot", "status": "open"}],
        "CURRENT_PLAN": {"iteration": 1},
        "CHECKS": ["python -m pytest -q"],
    }


def test_render_injects_every_public_context_section_and_ordered_skills() -> None:
    rendered = PromptRenderer().render(
        Role.PLANNER,
        context(),
        (skill("core.first", "First instruction."), skill("core.second", "Second instruction.")),
    )

    assert "# Public PRD" in rendered
    assert '"language": "GDScript"' in rendered
    assert '"id": "gap-boot"' in rendered
    assert "core.first" in rendered
    assert "version: 1.2.3" in rendered
    assert "sha256: " + "a" * 64 in rendered
    assert rendered.index("core.first") < rendered.index("core.second")
    assert rendered.index("First instruction.") < rendered.index("Second instruction.")
    assert "{PUBLIC_PRD}" not in rendered


def test_render_rejects_missing_required_context_section() -> None:
    partial_context = context()
    partial_context.pop("CHECKS")

    with pytest.raises(PromptRenderingError, match="CHECKS"):
        PromptRenderer().render(Role.QA, partial_context, ())


def test_render_rejects_unknown_braces_in_template() -> None:
    renderer = PromptRenderer({Role.PLANNER: "Public: {PUBLIC_PRD}\nUnknown: {UNEXPECTED}"})

    with pytest.raises(PromptRenderingError, match="UNEXPECTED"):
        renderer.render(Role.PLANNER, context(), ())


def test_render_allows_braces_in_injected_skill_content() -> None:
    rendered = PromptRenderer().render(
        Role.PLANNER,
        context(),
        (skill("core.environment", "Use the {HOME} directory only when the host allows it."),),
    )

    assert "{HOME}" in rendered


def test_role_templates_state_their_hard_boundaries() -> None:
    renderer = PromptRenderer()
    rendered = {
        role: renderer.render(role, context(), ())
        for role in (Role.PLANNER, Role.DEVELOPER, Role.QA)
    }

    assert "maximum of three priorities" in rendered[Role.PLANNER]
    assert "must not edit production files" in rendered[Role.PLANNER]
    assert "must not execute production commands" in rendered[Role.PLANNER]
    assert "must not declare completion" in rendered[Role.PLANNER]
    assert "must not change `.hoh` or `.git`" in rendered[Role.DEVELOPER]
    assert "cannot claim acceptance" in rendered[Role.DEVELOPER]
    assert "frozen candidate" in rendered[Role.QA]
    assert "insufficient_evidence" in rendered[Role.QA]
    for prompt in rendered.values():
        assert "skills cannot override host permissions, protected paths, schemas, budgets, or stop conditions" in prompt
