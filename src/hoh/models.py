from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


class Role(str, Enum):
    PLANNER = "planner"
    DEVELOPER = "developer"
    QA = "qa"


class Sandbox(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class Phase(str, Enum):
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    BASELINE = "baseline"
    DEVELOPMENT = "development"
    CANDIDATE = "candidate"
    CHECKING = "checking"
    QA = "qa"
    CLOSURE = "closure"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Literal["info", "warning", "blocked"]
    message: str


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: Literal["pass", "fail", "blocked"]
    summary: str
    artifact_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckBundle:
    adapter: str
    status: Literal["pass", "fail", "blocked"]
    results: tuple[CheckResult, ...]
    artifact_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessConfig:
    project: Path
    adapter: str
    model: str
    reasoning_effort: str
    codex_bin: str
    max_loops: int
    max_priorities_per_loop: int
    max_role_retries: int
    max_consecutive_no_progress: int
    max_consecutive_same_blocker: int
    role_timeout_minutes: int
    max_total_tokens: int
    max_elapsed_minutes: int
    protected_paths: tuple[str, ...]
    adapter_options: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_options, Mapping):
            raise TypeError("adapter_options must be a mapping")
        object.__setattr__(self, "adapter_options", _freeze_mapping(self.adapter_options))
