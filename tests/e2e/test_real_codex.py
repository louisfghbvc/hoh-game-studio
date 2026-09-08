from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hoh.backends.codex_exec import CodexExecBackend
from hoh.cli import build_services
from hoh.config import initialize_project, load_config


def _git(project: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def create_tiny_product(tmp_path: Path) -> Path:
    """Create a committed text-only product for the real-backend smoke test."""

    project = tmp_path / "real-codex-product"
    project.mkdir()
    _git(project, "init", "--initial-branch=main")
    (project / "product.txt").write_text("START\n", encoding="utf-8")
    initialize_project(project, "command", "gpt-5.3-codex", "low")

    state = project / ".hoh"
    (state / "prd.md").write_text(
        """# Tiny text product

Change `product.txt` from `START` to exactly `DONE` followed by a newline.
Keep the repository otherwise minimal.
""",
        encoding="utf-8",
    )
    (state / "requirements.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claims": [
                    {
                        "id": "product-text-done",
                        "description": "product.txt contains exactly DONE followed by a newline",
                        "required": True,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Keep the candidate check non-passing so an otherwise valid ordinary QA
    # result cannot enter the distinct full-release QA gate in this smoke test.
    verify_done = (
        "from pathlib import Path; "
        "assert Path('product.txt').read_text(encoding='utf-8') == 'DONE\\n'"
    )
    always_fail = "raise SystemExit('intentional one-loop smoke-test stop')"
    with (state / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            "checks = "
            + json.dumps(
                [
                    [sys.executable, "-c", verify_done],
                    [sys.executable, "-c", always_fail],
                ]
            )
            + "\nartifact_globs = [\"product.txt\"]"
            + "\ntimeout_seconds = 10\n"
        )

    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "initial",
    )
    return project


def run_real_single_loop(project: Path) -> dict[str, object]:
    """Compose the public production backend and run exactly one loop."""

    config = replace(load_config(project), max_loops=1)
    services = build_services(config, backend_name="codex-exec")
    assert isinstance(services.backend, CodexExecBackend)
    return services.orchestrator.run(max_loops=1)


@pytest.mark.real_codex
@pytest.mark.skipif(
    os.environ.get("HOH_REAL_CODEX_E2E") != "1",
    reason="set HOH_REAL_CODEX_E2E=1 to consume Codex quota",
)
def test_real_codex_produces_three_role_attempts(tmp_path: Path) -> None:
    project = create_tiny_product(tmp_path)
    result = run_real_single_loop(project)
    assert result["loops_completed"] == 1
    assert result["role_attempts"] == {"planner": 1, "developer": 1, "qa": 1}
    assert result["current_candidate"]
