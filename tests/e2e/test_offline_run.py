from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import hoh.cli as cli
from hoh.backends import AgentUsage, FakeAgentBackend, FakeResponse
from hoh.models import Role, Sandbox


def _git(project: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _product(tmp_path: Path) -> Path:
    project = tmp_path / "offline-product"
    project.mkdir()
    _git(project, "init", "--initial-branch=main")
    (project / "product.txt").write_text("original\n", encoding="utf-8")

    assert (
        cli.main(
            [
                "init",
                "--project",
                str(project),
                "--adapter",
                "command",
                "--model",
                "offline-model",
                "--reasoning-effort",
                "low",
            ]
        )
        == 0
    )
    state = project / ".hoh"
    (state / "prd.md").write_text(
        "# Offline product\n\nChange the product and retain executable proof.\n",
        encoding="utf-8",
    )
    (state / "requirements.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claims": [
                    {
                        "id": "claim-main",
                        "description": "The product records the changed behavior",
                        "required": True,
                    },
                    {
                        "id": "claim-polish",
                        "description": "The product has final polish",
                        "required": False,
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    check_script = (
        "from pathlib import Path; "
        "assert Path('product.txt').read_text(encoding='utf-8') == 'changed\\n'; "
        "assert Path('evidence/proof.txt').read_text(encoding='utf-8') == 'changed\\n'"
    )
    with (state / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            "checks = "
            + json.dumps([[sys.executable, "-c", check_script]])
            + "\nrequired_artifacts = [\"evidence/proof.txt\"]"
            + "\nartifact_globs = [\"evidence/proof.txt\"]"
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


def _plan() -> dict[str, object]:
    return {
        "iteration": 1,
        "objective": "Change the observable product",
        "priorities": [
            {
                "id": "priority-main",
                "title": "Change product",
                "implementation_target": "product.txt and evidence/proof.txt",
                "acceptance_claims": ["claim-main"],
                "source_gap_ids": [],
            }
        ],
        "preservation_constraints": [],
        "acceptance_gate": ["claim-main"],
    }


def _develop(request) -> None:
    (request.workspace / "product.txt").write_text("changed\n", encoding="utf-8")
    proof = request.workspace / "evidence" / "proof.txt"
    proof.parent.mkdir()
    proof.write_text("changed\n", encoding="utf-8")


def _developer_report() -> dict[str, object]:
    return {
        "summary": "change product and add executable proof",
        "completed_priority_ids": ["priority-main"],
        "self_tests": ["offline command check"],
        "known_gaps": ["final polish remains"],
    }


def _qa_response(request, *, complete: bool = False) -> dict[str, object]:
    candidate_matches = re.findall(
        r'"candidate_sha":\s*"([0-9a-f]{40})"', request.prompt
    )
    tree_matches = re.findall(
        r'"artifact_tree_sha256":\s*"([0-9a-f]{64})"', request.prompt
    )
    artifact = next(
        path
        for path in (
            "release-adapter/artifacts/evidence/proof.txt",
            "adapter/artifacts/evidence/proof.txt",
        )
        if path in request.prompt
    )
    hash_match = re.search(
        rf'"{re.escape(artifact)}":\s*"([0-9a-f]{{64}})"', request.prompt
    )
    assert candidate_matches and tree_matches and hash_match is not None
    return {
        "iteration": 1,
        "candidate_sha": candidate_matches[-1],
        "artifact_tree_sha256": tree_matches[-1],
        "qa_status": "pass",
        "product_complete": complete,
        "verified_records": [
            {
                "claim_id": "claim-main",
                "claim": "The product records the changed behavior",
                "observations": ["The retained proof contains the changed output."],
                "execution_records": [
                    {
                        "type": "text-check",
                        "path": artifact,
                        "sha256": hash_match.group(1),
                        "observation": "The command and retained proof both passed.",
                    }
                ],
                "preservation_requirement": "Keep the changed behavior working.",
            }
        ],
        "gap_records": (
            []
            if complete
            else [
                {
                    "claim_id": "claim-polish",
                    "severity": "major",
                    "impact": "The product still needs final polish.",
                    "observations": ["No final-polish evidence was requested."],
                    "recommended_update": "Add and verify the final polish.",
                    "validation_requirement": "Run a retained polish acceptance check.",
                }
            ]
        ),
        "planner_handoff": {
            "preservation_constraints": ["Preserve claim-main."],
            "update_targets": ["claim-polish"],
            "validation_requirements": ["Run the polish acceptance check."],
        },
    }


def test_offline_cli_runs_real_services_and_reports_structured_state(
    tmp_path: Path, capsys
) -> None:
    project = _product(tmp_path)
    capsys.readouterr()
    backend = FakeAgentBackend(
        [
            FakeResponse(_plan(), usage=AgentUsage(input_tokens=10, output_tokens=2)),
            FakeResponse(
                _developer_report(),
                usage=AgentUsage(input_tokens=20, output_tokens=3),
                on_run=_develop,
            ),
            FakeResponse(_qa_response, usage=AgentUsage(input_tokens=30, output_tokens=4)),
        ]
    )

    exit_code = cli.main(
        ["run", "--project", str(project), "--max-loops", "1"],
        backend=backend,
    )

    run_output = capsys.readouterr()
    assert exit_code == 4
    assert run_output.err == ""
    assert "Status: budget_exhausted" in run_output.out
    assert [request.role for request in backend.requests] == [
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
    ]
    assert [request.sandbox for request in backend.requests] == [
        Sandbox.READ_ONLY,
        Sandbox.WORKSPACE_WRITE,
        Sandbox.READ_ONLY,
    ]
    assert backend.requests[-1].workspace != project

    messages = _git(project, "log", "--format=%s").splitlines()
    assert sum(message.startswith("feat(loop-") for message in messages) == 1
    assert sum(message.startswith("test(loop-") for message in messages) == 1
    evidence_commit = _git(project, "rev-parse", "HEAD")
    candidate_commit = _git(project, "rev-parse", "HEAD^")
    assert evidence_commit != candidate_commit

    runs = tuple((project / ".hoh" / "runs").iterdir())
    assert len(runs) == 1
    receipts = tuple(runs[0].glob("loops/loop-*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["candidate_sha"] == candidate_commit
    manifest_path = receipts[0].parent / "adapter-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"]["adapter/artifacts/evidence/proof.txt"] == (
        hashlib.sha256((project / "evidence" / "proof.txt").read_bytes()).hexdigest()
    )

    assert cli.main(["status", "--project", str(project), "--json"]) == 0
    status_output = capsys.readouterr()
    assert status_output.err == ""
    status = json.loads(status_output.out)
    assert status["status"] == "budget_exhausted"
    assert status["current_candidate"] == candidate_commit
    assert status["role_attempts"] == {"developer": 1, "planner": 1, "qa": 1}
    assert status["total_tokens"] == 69
    assert status["guidance"] == f"hoh resume --run-id {status['run_id']}"

    assert cli.main(["report", "--project", str(project)]) == 0
    report_output = capsys.readouterr()
    assert report_output.err == ""
    assert "Status: budget_exhausted" in report_output.out
    assert f"Current candidate: {candidate_commit}" in report_output.out
    assert f"hoh resume --run-id {status['run_id']}" in report_output.out


def test_completed_offline_cli_recommends_candidate_not_evidence_commit(
    tmp_path: Path, capsys
) -> None:
    project = _product(tmp_path)
    capsys.readouterr()
    backend = FakeAgentBackend(
        [
            FakeResponse(_plan()),
            FakeResponse(_developer_report(), on_run=_develop),
            FakeResponse(lambda request: _qa_response(request, complete=True)),
            FakeResponse(lambda request: _qa_response(request, complete=True)),
        ]
    )

    assert (
        cli.main(
            ["run", "--project", str(project), "--max-loops", "1"],
            backend=backend,
        )
        == 0
    )
    run_output = capsys.readouterr()
    candidate_commit = _git(project, "rev-parse", "HEAD^")
    evidence_commit = _git(project, "rev-parse", "HEAD")

    assert [request.role for request in backend.requests] == [
        Role.PLANNER,
        Role.DEVELOPER,
        Role.QA,
        Role.QA,
    ]
    assert candidate_commit != evidence_commit
    assert f"git merge {candidate_commit}" in run_output.out
    assert f"git merge {evidence_commit}" not in run_output.out

    assert cli.main(["status", "--project", str(project), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "complete"
    assert status["current_candidate"] == candidate_commit
    assert status["best_candidate"] == candidate_commit
    assert status["guidance"] == f"git merge {candidate_commit}"

    run_dir = next((project / ".hoh" / "runs").iterdir())
    summary_path = run_dir / "run-summary.md"
    summary_before = summary_path.read_bytes()
    run_path = run_dir / "run.json"
    run_state = json.loads(run_path.read_text(encoding="utf-8"))
    forged = "f" * 40
    run_state.update(
        {
            "status": "complete",
            "reason": "forged complete",
            "current_candidate": forged,
            "best_candidate": forged,
            "loops": [
                {
                    "loop_index": 1,
                    "candidate_sha": forged,
                    "normalized_evidence": {
                        "candidate_sha": forged,
                        "product_complete": True,
                    },
                }
            ],
        }
    )
    run_path.write_text(json.dumps(run_state) + "\n", encoding="utf-8")

    assert cli.main(["status", "--project", str(project), "--json"]) == 5
    rejected_status = capsys.readouterr()
    assert rejected_status.out == ""
    assert "durable" in rejected_status.err
    assert f"git merge {forged}" not in rejected_status.err

    assert cli.main(["report", "--project", str(project)]) == 5
    rejected_report = capsys.readouterr()
    assert rejected_report.out == ""
    assert "durable" in rejected_report.err
    assert summary_path.read_bytes() == summary_before
