"""Shared request and result contracts for isolated agent invocations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hoh.models import Role, Sandbox


class BackendError(RuntimeError):
    """Base class for agent backend failures."""


class BackendTimeout(BackendError):
    """Raised when an agent invocation exceeds its time budget."""


class BackendProcessError(BackendError):
    """Raised when an agent process cannot start or exits unsuccessfully."""


class BackendProtocolError(BackendError):
    """Raised when an agent invocation violates the backend protocol."""


@dataclass(frozen=True)
class AgentRequest:
    role: Role
    prompt: str
    workspace: Path
    sandbox: Sandbox
    schema_path: Path
    model: str
    reasoning_effort: str
    timeout_seconds: int
    events_path: Path


@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass(frozen=True)
class AgentResult:
    response: dict[str, object]
    usage: AgentUsage
    return_code: int
    executable_version: str = "unknown"


class AgentBackend(Protocol):
    """Backend capable of completing one fresh agent invocation."""

    def run(self, request: AgentRequest) -> AgentResult:
        """Run one agent request and return its structured result."""
        ...
