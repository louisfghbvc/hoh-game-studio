from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping


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
