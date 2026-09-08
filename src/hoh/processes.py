"""Shared shell-free subprocess lifecycle primitives."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence


CLEANUP_TIMEOUT_SECONDS = 1.0
TREE_TERMINATION_TIMEOUT_SECONDS = 3.0


def process_group_options() -> dict[str, object]:
    """Return options that put one command and its descendants in a killable tree."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Best-effort terminate *process* and every descendant without a shell."""

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TREE_TERMINATION_TIMEOUT_SECONDS,
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


def wait_after_termination(process: subprocess.Popen[str]) -> int:
    """Reap a terminated parent within a fixed cleanup bound."""

    try:
        return process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            return process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            return process.returncode if process.returncode is not None else -1


def join_threads(
    threads: Sequence[threading.Thread],
    *,
    timeout: float = CLEANUP_TIMEOUT_SECONDS,
) -> bool:
    """Join a set of pipe workers against one shared deadline."""

    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return all(not thread.is_alive() for thread in threads)


def close_pipe_descriptors(process: subprocess.Popen[str]) -> None:
    """Unblock pipe readers after the process tree has been terminated."""

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
