"""Deterministic offline agent backend for orchestrator tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from hoh.backends.base import AgentRequest, AgentResult, AgentUsage


class UnexpectedInvocation(RuntimeError):
    """Raised when a fake backend receives more requests than configured."""


FakeResponseFactory = Callable[[AgentRequest], Mapping[str, object]]
FakeOnRun = Callable[[AgentRequest], None]


@dataclass(frozen=True)
class FakeResponse:
    response: Mapping[str, object] | FakeResponseFactory
    usage: AgentUsage = field(default_factory=AgentUsage)
    on_run: FakeOnRun | None = None


class FakeAgentBackend:
    """Return a fixed response sequence without launching external processes."""

    def __init__(
        self,
        responses: Sequence[FakeResponse],
        *,
        executable_version: str = "fake-agent/1",
    ) -> None:
        self._responses = list(responses)
        self._executable_version = executable_version
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if not self._responses:
            raise UnexpectedInvocation("no fake response remains for invocation")

        configured = self._responses.pop(0)
        if configured.on_run is not None:
            configured.on_run(request)
        response = (
            configured.response(request)
            if callable(configured.response)
            else configured.response
        )
        return AgentResult(
            dict(response), configured.usage, 0, self._executable_version
        )
