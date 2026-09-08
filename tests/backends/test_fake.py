from __future__ import annotations

from pathlib import Path

import pytest

from hoh.backends import (
    AgentRequest,
    AgentUsage,
    FakeAgentBackend,
    FakeResponse,
    UnexpectedInvocation,
)
from hoh.models import Role, Sandbox


def agent_request(tmp_path: Path, prompt: str = "first") -> AgentRequest:
    return AgentRequest(
        role=Role.DEVELOPER,
        prompt=prompt,
        workspace=tmp_path,
        sandbox=Sandbox.WORKSPACE_WRITE,
        schema_path=tmp_path / "response.schema.json",
        model="offline-model",
        reasoning_effort="low",
        timeout_seconds=1,
        events_path=tmp_path / "events.jsonl",
    )


def test_fake_returns_ordered_responses_and_records_requests(tmp_path: Path) -> None:
    first = agent_request(tmp_path, "first")
    second = agent_request(tmp_path, "second")
    backend = FakeAgentBackend(
        [
            FakeResponse({"sequence": 1}, AgentUsage(input_tokens=2)),
            FakeResponse({"sequence": 2}, AgentUsage(output_tokens=3)),
        ]
    )

    first_result = backend.run(first)
    assert first_result.response == {"sequence": 1}
    assert first_result.executable_version == "fake-agent/1"
    assert backend.run(second).response == {"sequence": 2}
    assert backend.requests == [first, second]


def test_fake_executable_version_is_configurable_and_deterministic(tmp_path: Path) -> None:
    backend = FakeAgentBackend(
        [FakeResponse({"status": "pass"})], executable_version="fixture-agent/7"
    )

    result = backend.run(agent_request(tmp_path))

    assert result.executable_version == "fixture-agent/7"


def test_fake_callable_can_bind_response_to_request(tmp_path: Path) -> None:
    request = agent_request(tmp_path, "candidate SHA is abc123")
    backend = FakeAgentBackend(
        [FakeResponse(lambda received: {"reviewed": received.prompt})]
    )

    result = backend.run(request)

    assert result.response == {"reviewed": "candidate SHA is abc123"}


def test_fake_on_run_may_create_product_changes(tmp_path: Path) -> None:
    request = agent_request(tmp_path)

    def create_change(received: AgentRequest) -> None:
        (received.workspace / "product.txt").write_text("changed", encoding="utf-8")

    backend = FakeAgentBackend([FakeResponse({"changed": True}, on_run=create_change)])

    result = backend.run(request)

    assert result.response == {"changed": True}
    assert (tmp_path / "product.txt").read_text(encoding="utf-8") == "changed"


def test_fake_raises_when_responses_are_exhausted(tmp_path: Path) -> None:
    request = agent_request(tmp_path)
    backend = FakeAgentBackend([])

    with pytest.raises(UnexpectedInvocation, match="no fake response"):
        backend.run(request)

    assert backend.requests == [request]
