"""Shell-free backend for one-shot ``codex exec`` invocations."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from hoh.backends.base import (
    AgentRequest,
    AgentResult,
    AgentUsage,
    BackendProcessError,
    BackendProtocolError,
    BackendTimeout,
)
from hoh.models import Role, Sandbox


_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|AUTH", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:TOKEN|KEY|SECRET|PASSWORD|AUTH)[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SECRET_OPTION = re.compile(
    r"(?i)(--?[A-Za-z0-9_.-]*(?:TOKEN|KEY|SECRET|PASSWORD|AUTH)[A-Za-z0-9_.-]*)"
    r"(\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


class CodexExecBackend:
    """Run every agent request in a fresh, explicitly sandboxed Codex process."""

    def __init__(self, executable: str = "codex") -> None:
        self._executable = executable

    def build_command(self, request: AgentRequest) -> list[str]:
        """Build the argv list for a shell-free Codex invocation."""

        self._validate_request(request)
        return [
            self._executable,
            "exec",
            "--json",
            "--ephemeral",
            "--model",
            request.model,
            "-c",
            f'model_reasoning_effort="{request.reasoning_effort}"',
            "--sandbox",
            request.sandbox.value,
            "--output-schema",
            str(request.schema_path),
            "-",
        ]

    def run(self, request: AgentRequest) -> AgentResult:
        """Execute one request, retaining JSONL events as they arrive."""

        command = self.build_command(request)
        environment = os.environ.copy()
        request.events_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            process = subprocess.Popen(
                command,
                cwd=request.workspace,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except OSError as error:
            raise BackendProcessError("could not start Codex executable") from error

        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise BackendProcessError("could not establish Codex process pipes")

        events: list[dict[str, object]] = []
        protocol_errors: list[str] = []
        stderr_chunks: list[str] = []
        events_file = request.events_path.open("w", encoding="utf-8", newline="")

        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, events_file, events, protocol_errors),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr, stderr_chunks),
            daemon=True,
        )
        stdin_thread = threading.Thread(
            target=self._write_stdin,
            args=(process.stdin, request.prompt),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        stdin_thread.start()

        timed_out = False
        try:
            return_code = process.wait(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            return_code = process.wait()
        finally:
            stdin_thread.join()
            stdout_thread.join()
            stderr_thread.join()
            events_file.close()

        if timed_out:
            raise BackendTimeout(
                f"Codex invocation timed out after {request.timeout_seconds} seconds"
            )

        if return_code != 0:
            detail = "".join(stderr_chunks).strip()
            detail = self._redact(detail, environment)
            suffix = f": {detail}" if detail else ""
            raise BackendProcessError(
                f"Codex process exited with exit status {return_code}{suffix}"
            )

        if protocol_errors:
            raise BackendProtocolError(protocol_errors[0])

        response = self._final_response(events)
        usage = self._final_usage(events)
        return AgentResult(response, usage, return_code)

    @staticmethod
    def _validate_request(request: AgentRequest) -> None:
        if request.role in {Role.PLANNER, Role.QA}:
            if request.sandbox is not Sandbox.READ_ONLY:
                raise BackendProtocolError(
                    f"{request.role.value} must be read-only"
                )
        elif request.role is Role.DEVELOPER:
            if request.sandbox is not Sandbox.WORKSPACE_WRITE:
                raise BackendProtocolError(
                    "developer must use workspace-write"
                )
        else:
            raise BackendProtocolError("unsupported agent role")

        if request.sandbox not in {Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE}:
            raise BackendProtocolError("unsupported Codex sandbox")
        if not request.workspace.is_dir():
            raise BackendProtocolError("agent workspace does not exist")
        if not request.schema_path.is_file():
            raise BackendProtocolError("agent output schema does not exist")
        if request.timeout_seconds <= 0:
            raise BackendProtocolError("agent timeout must be positive")

    @staticmethod
    def _write_stdin(stream: IO[str], prompt: str) -> None:
        try:
            stream.write(prompt)
            stream.close()
        except (BrokenPipeError, OSError):
            pass

    @staticmethod
    def _read_stderr(stream: IO[str], chunks: list[str]) -> None:
        try:
            for chunk in iter(lambda: stream.read(8192), ""):
                chunks.append(chunk)
        finally:
            stream.close()

    @staticmethod
    def _read_stdout(
        stream: IO[str],
        events_file: IO[str],
        events: list[dict[str, object]],
        protocol_errors: list[str],
    ) -> None:
        try:
            for line_number, raw_line in enumerate(stream, start=1):
                retained_line = raw_line if raw_line.endswith("\n") else raw_line + "\n"
                events_file.write(retained_line)
                events_file.flush()
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    protocol_errors.append(
                        f"malformed JSONL event on line {line_number}"
                    )
                    continue
                if not isinstance(event, dict):
                    protocol_errors.append(
                        f"JSONL event on line {line_number} must be an object"
                    )
                    continue
                events.append(event)
        except OSError:
            protocol_errors.append("could not read Codex JSONL output")
        finally:
            stream.close()

    @classmethod
    def _final_response(cls, events: Sequence[Mapping[str, object]]) -> dict[str, object]:
        for event in reversed(events):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, Mapping) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                raise BackendProtocolError("final agent response text is missing")
            try:
                response = json.loads(text)
            except json.JSONDecodeError as error:
                raise BackendProtocolError("final agent response is malformed JSON") from error
            if not isinstance(response, dict):
                raise BackendProtocolError("final agent response must be a JSON object")
            return response
        raise BackendProtocolError("final agent response is missing")

    @classmethod
    def _final_usage(cls, events: Sequence[Mapping[str, object]]) -> AgentUsage:
        completed = [event for event in events if event.get("type") == "turn.completed"]
        if not completed:
            raise BackendProtocolError("turn.completed event is missing")
        raw_usage = completed[-1].get("usage")
        if not isinstance(raw_usage, Mapping):
            raise BackendProtocolError("final turn.completed usage is missing")
        return AgentUsage(
            input_tokens=cls._usage_integer(raw_usage, "input_tokens"),
            cached_input_tokens=cls._usage_integer(raw_usage, "cached_input_tokens"),
            output_tokens=cls._usage_integer(raw_usage, "output_tokens"),
            reasoning_output_tokens=cls._usage_integer(
                raw_usage, "reasoning_output_tokens", default=0
            ),
        )

    @staticmethod
    def _usage_integer(
        usage: Mapping[str, object], name: str, *, default: int | None = None
    ) -> int:
        value = usage.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BackendProtocolError(
                f"final turn.completed usage field {name} is invalid"
            )
        return value

    @staticmethod
    def _redact(detail: str, environment: Mapping[str, str]) -> str:
        redacted = detail
        secret_values = sorted(
            {
                value
                for name, value in environment.items()
                if value and _SECRET_NAME.search(name)
            },
            key=len,
            reverse=True,
        )
        for value in secret_values:
            redacted = redacted.replace(value, "<redacted>")
        redacted = _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", redacted)
        return _SECRET_OPTION.sub(r"\1\2<redacted>", redacted)
