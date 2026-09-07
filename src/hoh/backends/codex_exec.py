"""Shell-free backend for one-shot ``codex exec`` invocations."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
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
    r"(?i)(?P<prefix>[\"']?[A-Za-z0-9_.-]*"
    r"(?:TOKEN|KEY|SECRET|PASSWORD|AUTH)[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_SECRET_OPTION = re.compile(
    r"(?i)(?P<prefix>--?[A-Za-z0-9_.-]*"
    r"(?:TOKEN|KEY|SECRET|PASSWORD|AUTH)[A-Za-z0-9_.-]*\s+)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_CLEANUP_TIMEOUT_SECONDS = 1.0
_TREE_TERMINATION_TIMEOUT_SECONDS = 3.0
_VERSION_TIMEOUT_SECONDS = 5.0


class CodexExecBackend:
    """Run every agent request in a fresh, explicitly sandboxed Codex process."""

    def __init__(self, executable: str = "codex") -> None:
        self._executable = (
            str(Path(executable).resolve())
            if os.path.dirname(executable)
            else executable
        )

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
            str(request.schema_path.resolve()),
            "-",
        ]

    def run(self, request: AgentRequest) -> AgentResult:
        """Execute one request, retaining JSONL events as they arrive."""

        command = self.build_command(request)
        environment = os.environ.copy()
        try:
            request.events_path.parent.mkdir(parents=True, exist_ok=True)
            events_file = request.events_path.open(
                "w", encoding="utf-8", newline=""
            )
        except OSError:
            raise BackendProcessError("could not open Codex event sink") from None

        try:
            executable_version = self._probe_version(request, environment)
        except BaseException:
            events_file.close()
            raise

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
                **self._process_group_options(),
            )
        except OSError:
            events_file.close()
            raise BackendProcessError("could not start Codex executable") from None

        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._terminate_process_tree(process)
            self._wait_after_termination(process)
            events_file.close()
            raise BackendProcessError("could not establish Codex process pipes")

        events: list[dict[str, object]] = []
        protocol_errors: list[str] = []
        stderr_chunks: list[str] = []

        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(
                process.stdout,
                events_file,
                events,
                protocol_errors,
                environment,
            ),
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
            self._terminate_process_tree(process)
            return_code = self._wait_after_termination(process)
        finally:
            threads = (stdin_thread, stdout_thread, stderr_thread)
            if not self._join_threads(threads):
                self._terminate_process_tree(process)
                self._close_pipe_descriptors(process)
                self._join_threads(threads, timeout=0.25)
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
        return AgentResult(response, usage, return_code, executable_version)

    def _probe_version(
        self, request: AgentRequest, environment: Mapping[str, str]
    ) -> str:
        timeout = min(float(request.timeout_seconds), _VERSION_TIMEOUT_SECONDS)
        command = [self._executable, "--version"]
        try:
            process = subprocess.Popen(
                command,
                cwd=request.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **self._process_group_options(),
            )
        except OSError:
            raise BackendProcessError(
                "could not start Codex executable version probe"
            ) from None

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            self._close_pipe_descriptors(process)
            try:
                process.communicate(timeout=_CLEANUP_TIMEOUT_SECONDS)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self._wait_after_termination(process)
            raise BackendTimeout("Codex executable version probe timed out") from None

        if process.returncode != 0:
            detail = (stderr or stdout).strip()
            detail = self._redact(detail, environment)
            suffix = f": {detail}" if detail else ""
            raise BackendProcessError(
                "Codex executable version probe exited with exit status "
                f"{process.returncode}{suffix}"
            )
        output = (stdout or stderr).strip()
        if not output:
            raise BackendProtocolError("Codex executable version is missing")
        return self._redact(output.splitlines()[0], environment)

    @staticmethod
    def _process_group_options() -> dict[str, object]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_TREE_TERMINATION_TIMEOUT_SECONDS,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _wait_after_termination(process: subprocess.Popen[str]) -> int:
        try:
            return process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                return process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                return process.returncode if process.returncode is not None else -1

    @staticmethod
    def _join_threads(
        threads: Sequence[threading.Thread],
        *,
        timeout: float = _CLEANUP_TIMEOUT_SECONDS,
    ) -> bool:
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in threads)

    @staticmethod
    def _close_pipe_descriptors(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                descriptor = stream.fileno()
            except (OSError, ValueError):
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

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
        except (BrokenPipeError, OSError, ValueError):
            pass

    @staticmethod
    def _read_stderr(stream: IO[str], chunks: list[str]) -> None:
        try:
            for chunk in iter(lambda: stream.read(8192), ""):
                chunks.append(chunk)
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @classmethod
    def _read_stdout(
        cls,
        stream: IO[str],
        events_file: IO[str],
        events: list[dict[str, object]],
        protocol_errors: list[str],
        environment: Mapping[str, str],
    ) -> None:
        try:
            for line_number, raw_line in enumerate(stream, start=1):
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    events_file.write(cls._redact(raw_line.rstrip("\r\n"), environment))
                    events_file.write("\n")
                    events_file.flush()
                    protocol_errors.append(
                        f"malformed JSONL event on line {line_number}"
                    )
                    continue
                retained_event = cls._redact_value(event, environment)
                events_file.write(
                    json.dumps(retained_event, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                events_file.flush()
                if not isinstance(event, dict):
                    protocol_errors.append(
                        f"JSONL event on line {line_number} must be an object"
                    )
                    continue
                events.append(event)
        except (OSError, ValueError):
            protocol_errors.append("could not read Codex JSONL output")
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

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
        redacted = _SECRET_ASSIGNMENT.sub(
            CodexExecBackend._redacted_match, redacted
        )
        return _SECRET_OPTION.sub(CodexExecBackend._redacted_match, redacted)

    @staticmethod
    def _redacted_match(match: re.Match[str]) -> str:
        value = match.group("value")
        if len(value) >= 2 and value[0] in {"\"", "'"} and value[-1] == value[0]:
            replacement = f"{value[0]}<redacted>{value[0]}"
        else:
            replacement = "<redacted>"
        return match.group("prefix") + replacement

    @classmethod
    def _redact_value(
        cls, value: object, environment: Mapping[str, str]
    ) -> object:
        if isinstance(value, Mapping):
            return {
                key: (
                    "<redacted>"
                    if _SECRET_NAME.search(str(key))
                    else cls._redact_value(item, environment)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact_value(item, environment) for item in value]
        if isinstance(value, str):
            return cls._redact(value, environment)
        return value
