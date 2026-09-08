from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


def test_command_resolves_relative_schema_before_changing_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = agent_request(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative_request = replace(request, schema_path=Path("response.schema.json"))

    command = CodexExecBackend("codex").build_command(relative_request)

    assert command[command.index("--output-schema") + 1] == str(
        (tmp_path / "response.schema.json").resolve()
    )


def test_backend_resolves_relative_executable_with_directory_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "tools" / "codex"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)

    backend = CodexExecBackend(str(Path("tools") / "codex"))
    monkeypatch.chdir(workspace)
    request = agent_request(tmp_path)

    assert backend.build_command(request)[0] == str(executable.resolve())


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
    assert result.executable_version.startswith("Python ")
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


def test_process_errors_redact_complete_spaced_bearer_option_values(
    tmp_path: Path,
) -> None:
    request = agent_request(tmp_path)
    diagnostic = (
        "command: tool --auth Bearer super-secret-token "
        "--token Bearer second-secret-token "
        "Authorization=Bearer third-secret-token --model safe"
    )
    backend = executable_backend(
        request.workspace,
        f"import sys\nsys.stderr.write({diagnostic!r})\nsys.exit(9)\n",
    )

    with pytest.raises(BackendProcessError) as raised:
        backend.run(request)

    message = str(raised.value)
    for secret in (
        "super-secret-token",
        "second-secret-token",
        "third-secret-token",
        "super-secret",
        "second-secret",
        "third-secret",
    ):
        assert secret not in message
    assert "--auth <redacted>" in message
    assert "--token <redacted>" in message
    assert "Authorization=<redacted>" in message


def test_process_errors_redact_quoted_and_equal_secret_values(tmp_path: Path) -> None:
    request = agent_request(tmp_path)
    diagnostic = (
        'AUTH="quoted secret value" '
        "--auth=equal-secret --password 'other secret value'"
    )
    backend = executable_backend(
        request.workspace,
        f"import sys\nsys.stderr.write({diagnostic!r})\nsys.exit(9)\n",
    )

    with pytest.raises(BackendProcessError) as raised:
        backend.run(request)

    message = str(raised.value)
    assert "quoted secret value" not in message
    assert "equal-secret" not in message
    assert "other secret value" not in message
    assert 'AUTH="<redacted>"' in message
    assert "--auth=<redacted>" in message
    assert "--password '<redacted>'" in message


def test_process_errors_redact_the_complete_authorization_bearer_value(
    tmp_path: Path,
) -> None:
    """Redacting only the `Bearer` word must leave this regression failing."""

    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        "import sys\n"
        'sys.stderr.write("Authorization: Bearer super-secret-token")\n'
        "sys.exit(9)\n",
    )

    with pytest.raises(BackendProcessError) as raised:
        backend.run(request)

    message = str(raised.value)
    assert "super-secret-token" not in message
    assert "super-secret" not in message
    assert "secret-token" not in message
    assert "Authorization: <redacted>" in message


def test_retained_events_redact_structured_secrets_without_changing_response(
    tmp_path: Path,
) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        """import json
response = {"status": "pass", "auth_token": "response secret"}
print(json.dumps({"type": "item.completed", "api_key": "structured secret", "nested": {"password": "nested secret"}, "detail": 'AUTH="quoted secret value" --auth=equal-secret', "item": {"type": "agent_message", "text": json.dumps(response)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}))
""",
    )

    result = backend.run(request)

    assert result.response == {"status": "pass", "auth_token": "response secret"}
    persisted = [
        json.loads(line)
        for line in request.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted[0]["api_key"] == "<redacted>"
    assert persisted[0]["nested"]["password"] == "<redacted>"
    assert "quoted secret value" not in persisted[0]["detail"]
    assert "equal-secret" not in persisted[0]["detail"]
    assert json.loads(persisted[0]["item"]["text"])["auth_token"] == "<redacted>"


def test_retained_nested_error_json_redacts_bearer_without_overredacting_text(
    tmp_path: Path,
) -> None:
    """Nested event/error strings must retain neither a full nor partial credential."""

    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        """import json
response = {"status": "pass"}
detail = "Authorization: Bearer super-secret-token"
print(json.dumps({"type": "item.completed", "error": {"message": detail, "json": json.dumps({"Authorization": "Bearer super-secret-token"})}, "note": "The bearer of good news is ordinary text.", "item": {"type": "agent_message", "text": json.dumps(response)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}))
""",
    )

    backend.run(request)

    retained = request.events_path.read_text(encoding="utf-8")
    assert "super-secret-token" not in retained
    assert "super-secret" not in retained
    assert "secret-token" not in retained
    event = json.loads(retained.splitlines()[0])
    assert event["note"] == "The bearer of good news is ordinary text."
    assert json.loads(event["error"]["json"])["Authorization"] == "<redacted>"


def test_retained_nested_event_redacts_spaced_bearer_options_without_plain_text_loss(
    tmp_path: Path,
) -> None:
    request = agent_request(tmp_path)
    backend = executable_backend(
        request.workspace,
        """import json
response = {"status": "pass"}
detail = "tool --auth Bearer super-secret-token"
nested = json.dumps({"command": "tool --token Bearer second-secret-token", "header": "Authorization: Bearer third-secret-token"})
print(json.dumps({"type": "item.completed", "error": {"detail": detail, "nested": nested}, "note": "The bearer of good news is ordinary text.", "item": {"type": "agent_message", "text": json.dumps(response)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}))
""",
    )

    backend.run(request)

    retained = request.events_path.read_text(encoding="utf-8")
    for secret in (
        "super-secret-token",
        "second-secret-token",
        "third-secret-token",
        "super-secret",
        "second-secret",
        "third-secret",
    ):
        assert secret not in retained
    event = json.loads(retained.splitlines()[0])
    assert event["error"]["detail"] == "tool --auth <redacted>"
    assert json.loads(event["error"]["nested"]) == {
        "command": "tool --token <redacted>",
        "header": "Authorization: <redacted>",
    }
    assert event["note"] == "The bearer of good news is ordinary text."


def test_timeout_raises_distinct_error(tmp_path: Path) -> None:
    request = replace(agent_request(tmp_path), timeout_seconds=1)
    backend = executable_backend(
        request.workspace,
        "import time\ntime.sleep(5)\n",
    )

    with pytest.raises(BackendTimeout, match="timed out"):
        backend.run(request)


def test_timeout_terminates_descendant_holding_inherited_pipes(tmp_path: Path) -> None:
    request = replace(agent_request(tmp_path), timeout_seconds=1)
    child_pid_path = tmp_path / "child.pid"
    backend = executable_backend(
        request.workspace,
        f"""import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], stdout=sys.stdout, stderr=sys.stderr)
open({json.dumps(str(child_pid_path))}, "w", encoding="utf-8").write(str(child.pid))
time.sleep(5)
""",
    )

    started = time.monotonic()
    with pytest.raises(BackendTimeout):
        backend.run(request)
    elapsed = time.monotonic() - started

    assert elapsed < 4.5
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert _wait_until_process_exits(child_pid)


def test_event_sink_failure_does_not_launch_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = agent_request(tmp_path)
    request.events_path.mkdir()
    launched = False

    def unexpected_launch(*args: object, **kwargs: object) -> object:
        nonlocal launched
        launched = True
        raise AssertionError("process launched before event sink validation")

    monkeypatch.setattr("hoh.backends.codex_exec.subprocess.Popen", unexpected_launch)
    monkeypatch.setattr("hoh.backends.codex_exec.subprocess.run", unexpected_launch)

    with pytest.raises(BackendProcessError, match="event sink"):
        CodexExecBackend("codex").run(request)

    assert launched is False


def test_executable_version_probe_is_shell_free_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = agent_request(tmp_path)
    observed: dict[str, object] = {}

    class TimedOutProbe:
        pid = 123
        returncode: int | None = None
        stdin = None
        stdout = None
        stderr = None

        def communicate(self, timeout: float) -> tuple[str, str]:
            observed["timeout"] = timeout
            raise subprocess.TimeoutExpired(observed["command"], timeout)

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float) -> int:
            self.returncode = -9
            return self.returncode

    def timed_out_probe(command: list[str], **kwargs: object) -> TimedOutProbe:
        observed["command"] = command
        observed.update(kwargs)
        return TimedOutProbe()

    monkeypatch.setattr("hoh.backends.codex_exec.subprocess.Popen", timed_out_probe)
    monkeypatch.setattr(
        CodexExecBackend,
        "_terminate_process_tree",
        lambda self, process: process.kill(),
    )

    with pytest.raises(BackendTimeout, match="version probe"):
        CodexExecBackend("codex").run(request)

    assert observed["command"] == ["codex", "--version"]
    assert observed["shell"] is False
    assert 0 < observed["timeout"] <= request.timeout_seconds


def test_missing_executable_is_a_process_error(tmp_path: Path) -> None:
    request = agent_request(tmp_path)

    with pytest.raises(BackendProcessError, match="could not start"):
        CodexExecBackend(str(tmp_path / "missing-codex")).run(request)


def _wait_until_process_exits(pid: int) -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
        )
        return f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
