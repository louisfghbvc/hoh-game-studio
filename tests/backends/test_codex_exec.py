from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hoh.backends import (
    AgentRequest,
    AgentUsage,
    BackendProcessError,
    BackendProtocolError,
    BackendTimeout,
    CodexExecBackend,
)
from hoh.models import Role, Sandbox


def agent_request(
    tmp_path: Path,
    role: Role = Role.DEVELOPER,
    sandbox: Sandbox = Sandbox.WORKSPACE_WRITE,
) -> AgentRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    schema_path = tmp_path / "response.schema.json"
    schema_path.write_text('{"type": "object"}', encoding="utf-8")
    return AgentRequest(
        role=role,
        prompt="Return the requested JSON.",
        workspace=workspace,
        sandbox=sandbox,
        schema_path=schema_path,
        model="test-model",
        reasoning_effort="high",
        timeout_seconds=2,
        events_path=tmp_path / "events.jsonl",
    )


def executable_backend(workspace: Path, body: str) -> CodexExecBackend:
    (workspace / "exec").write_text(body, encoding="utf-8")
    return CodexExecBackend(sys.executable)


def test_developer_command_uses_workspace_write(tmp_path: Path) -> None:
    request = agent_request(tmp_path, Role.DEVELOPER, Sandbox.WORKSPACE_WRITE)

    command = CodexExecBackend("codex").build_command(request)

    assert command[:3] == ["codex", "exec", "--json"]
    assert "--ephemeral" in command
    assert command[command.index("--model") + 1] == "test-model"
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--output-schema") + 1] == str(request.schema_path)
    assert command[-1] == "-"
    assert "--full-auto" not in command
    assert "danger-full-access" not in command


@pytest.mark.parametrize("role", [Role.PLANNER, Role.QA])
def test_read_only_role_cannot_request_write_sandbox(
    tmp_path: Path, role: Role
) -> None:
    request = agent_request(tmp_path, role, Sandbox.WORKSPACE_WRITE)

    with pytest.raises(BackendProtocolError, match=rf"{role.value} must be read-only"):
        CodexExecBackend("codex").run(request)


def test_developer_cannot_request_read_only_sandbox(tmp_path: Path) -> None:
    request = agent_request(tmp_path, Role.DEVELOPER, Sandbox.READ_ONLY)

    with pytest.raises(
        BackendProtocolError, match="developer must use workspace-write"
    ):
        CodexExecBackend("codex").run(request)


def test_run_passes_prompt_through_stdin_and_retains_jsonl(tmp_path: Path) -> None:
    request = replace(agent_request(tmp_path), prompt="prompt with ; shell syntax")
    backend = executable_backend(
        request.workspace,
        """import json, sys
prompt = sys.stdin.read()
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"prompt": prompt})}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 7, "reasoning_output_tokens": 2}}), flush=True)
""",
    )

    result = backend.run(request)

    assert result.response == {"prompt": "prompt with ; shell syntax"}
    assert result.usage == AgentUsage(11, 3, 7, 2)
    assert result.return_code == 0
    events = [
        json.loads(line)
        for line in request.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in events] == [
        "item.completed",
        "turn.completed",
    ]


def test_usage_comes_from_final_turn_completed_event(tmp_path: Path) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        """import json
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{\\\"status\\\": \\\"pass\\\"}"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 9}}))
""",
    )

    result = backend.run(request)

    assert result.usage == AgentUsage(20, 5, 9, 0)


def test_malformed_jsonl_is_a_protocol_error_and_is_retained(tmp_path: Path) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        """print("not-json", flush=True)
""",
    )

    with pytest.raises(BackendProtocolError, match="malformed JSONL"):
        backend.run(request)

    assert request.events_path.read_text(encoding="utf-8") == "not-json\n"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            'import json\nprint(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}))\n',
            "final agent response",
        ),
        (
            'import json\nprint(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}))\n',
            "turn.completed",
        ),
    ],
)
def test_missing_required_output_is_a_protocol_error(
    tmp_path: Path, body: str, message: str
) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(request.workspace, body)

    with pytest.raises(BackendProtocolError, match=message):
        backend.run(request)


def test_nonzero_exit_is_a_process_error(tmp_path: Path) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        'import sys\nsys.stderr.write("fixture failed")\nsys.exit(7)\n',
    )

    with pytest.raises(BackendProcessError, match="exit status 7.*fixture failed"):
        backend.run(request)


def test_process_errors_redact_secret_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = agent_request(tmp_path)
    monkeypatch.setenv("HOH_SECRET_KEY", "do-not-expose-this-value")
    backend = executable_backend(
        request.workspace,
        'import os, sys\nsys.stderr.write(os.environ["HOH_SECRET_KEY"])\nsys.exit(8)\n',
    )

    with pytest.raises(BackendProcessError) as raised:
        backend.run(request)

    assert "do-not-expose-this-value" not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_process_errors_redact_secret_command_option_values(tmp_path: Path) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        'import sys\nsys.stderr.write("command: tool --auth bearer-value --model safe")\nsys.exit(9)\n',
    )

    with pytest.raises(BackendProcessError) as raised:
        backend.run(request)

    assert "bearer-value" not in str(raised.value)
    assert "--auth <redacted>" in str(raised.value)


def test_timeout_raises_distinct_error(tmp_path: Path) -> None:
    request = replace(agent_request(tmp_path), timeout_seconds=1)
    backend = executable_backend(
        request.workspace,
        "import time\ntime.sleep(5)\n",
    )

    with pytest.raises(BackendTimeout, match="timed out"):
        backend.run(request)


def test_missing_executable_is_a_process_error(tmp_path: Path) -> None:
    request = agent_request(tmp_path)

    with pytest.raises(BackendProcessError, match="could not start"):
        CodexExecBackend(str(tmp_path / "missing-codex")).run(request)
