"""Agent backend interfaces and implementations."""

from hoh.backends.base import (
    AgentBackend,
    AgentRequest,
    AgentResult,
    AgentUsage,
    BackendError,
    BackendProcessError,
    BackendProtocolError,
    BackendTimeout,
)
from hoh.backends.codex_exec import CodexExecBackend
from hoh.backends.fake import FakeAgentBackend, FakeResponse, UnexpectedInvocation

__all__ = [
    "AgentBackend",
    "AgentRequest",
    "AgentResult",
    "AgentUsage",
    "BackendError",
    "BackendProcessError",
    "BackendProtocolError",
    "BackendTimeout",
    "CodexExecBackend",
    "FakeAgentBackend",
    "FakeResponse",
    "UnexpectedInvocation",
]
