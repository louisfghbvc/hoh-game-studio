from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib import resources

from hoh.models import Role
from hoh.skills.registry import SkillDocument


_CONTEXT_SECTIONS = (
    "PUBLIC_PRD",
    "PROJECT_SUMMARY",
    "PREVIOUS_EVIDENCE",
    "ISSUE_LEDGER",
    "CURRENT_PLAN",
    "CHECKS",
)
_SECTION_NAMES = frozenset((*_CONTEXT_SECTIONS, "SKILLS"))
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z_]*)\}")


class PromptRenderingError(ValueError):
    """Raised when a role prompt cannot be rendered into complete public context."""


class PromptRenderer:
    """Render fresh, deterministic role prompts from public context and selected skills."""

    def __init__(self, templates: Mapping[Role, str] | None = None) -> None:
        self._templates = dict(templates) if templates is not None else self._load_templates()

    def render(
        self,
        role: Role,
        context: Mapping[str, object],
        skills: tuple[SkillDocument, ...],
    ) -> str:
        try:
            template = self._templates[role]
        except KeyError as error:
            raise PromptRenderingError(f"missing template for role: {role.value}") from error

        missing = [section for section in _CONTEXT_SECTIONS if section not in context]
        if missing:
            raise PromptRenderingError(
                "missing required prompt section: " + ", ".join(missing)
            )

        rendered_context = {
            section: _render_value(context[section]) for section in _CONTEXT_SECTIONS
        }
        rendered_context["SKILLS"] = _render_skills(skills)
        return _render_template(template, rendered_context)

    @staticmethod
    def _load_templates() -> dict[Role, str]:
        root = resources.files("hoh").joinpath("resources", "prompts")
        return {
            role: root.joinpath(f"{role.value}.md").read_text(encoding="utf-8")
            for role in Role
        }


def _render_template(template: str, sections: Mapping[str, str]) -> str:
    unknown = set(_PLACEHOLDER.findall(template)) - _SECTION_NAMES
    if unknown:
        raise PromptRenderingError(
            "unknown prompt section: " + ", ".join(sorted(unknown))
        )

    def replace(match: re.Match[str]) -> str:
        section = match.group(1)
        try:
            return sections[section]
        except KeyError as error:
            raise PromptRenderingError(f"missing required prompt section: {section}") from error

    rendered = _PLACEHOLDER.sub(replace, template)
    if _PLACEHOLDER.search(rendered):
        raise PromptRenderingError("unknown braces remain after prompt rendering")
    return rendered


def _render_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _render_skills(skills: tuple[SkillDocument, ...]) -> str:
    if not skills:
        return "No skills selected."
    return "\n\n".join(
        "\n".join(
            (
                f"### Skill: {skill.skill_id}",
                f"version: {skill.version}",
                f"sha256: {skill.sha256}",
                "content:",
                skill.content,
            )
        )
        for skill in skills
    )
