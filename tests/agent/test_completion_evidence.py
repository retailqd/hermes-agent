from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent.completion_evidence import (
    CompletionEvidenceError,
    create_plan_completion_manifest,
    verify_completion_manifest,
)
from agent.verification_evidence import mark_workspace_edited, record_terminal_result


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(path), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


def _plan(path: Path) -> tuple[Path, str]:
    content = "# Accepted plan\n"
    path.write_text(content, encoding="utf-8")
    import hashlib

    return path, hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def test_completion_manifest_is_bounded_hashed_and_verifiable(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan, plan_sha = _plan(tmp_path / "plan.md")
    changed = tmp_path / "src.py"
    changed.write_text("print('ok')\n", encoding="utf-8")
    mark_workspace_edited(session_id="s1", cwd=tmp_path, paths=[str(changed)])
    record_terminal_result(
        command="python -m pytest -q",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
        output="1 passed",
    )

    path, digest = create_plan_completion_manifest(
        session_id="s1",
        cwd=tmp_path,
        objective="implement safely",
        approval_id="approval",
        plan_artifact_path=str(plan),
        plan_artifact_sha256=plan_sha,
        final_response="done",
    )

    manifest = verify_completion_manifest(path)
    assert manifest["manifest_sha256"] == digest
    assert manifest["verification"]["status"] == "passed"
    assert manifest["changed_artifacts"] == [str(changed)]
    serialized = json.dumps(manifest)
    assert "implement safely" not in serialized
    assert 'approval"' not in serialized


def test_completion_manifest_accepts_fresh_venv_unittest_evidence(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='unittest-fixture'\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cli.py").write_text("import unittest\n", encoding="utf-8")
    plan, plan_sha = _plan(tmp_path / "plan.md")
    changed = tmp_path / "src.py"
    changed.write_text("print('ok')\n", encoding="utf-8")
    mark_workspace_edited(session_id="s1", cwd=tmp_path, paths=[str(changed)])
    evidence = record_terminal_result(
        command=".venv/bin/python -m unittest discover -s tests -v",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
        output="Ran 22 tests\nOK",
    )

    assert evidence is not None
    path, _ = create_plan_completion_manifest(
        session_id="s1",
        cwd=tmp_path,
        objective="implement safely",
        approval_id="approval",
        plan_artifact_path=str(plan),
        plan_artifact_sha256=plan_sha,
        final_response="done",
    )

    manifest = verify_completion_manifest(path)
    assert manifest["verification"]["status"] == "passed"
    assert (
        manifest["verification"]["check"]["canonical_command"]
        == "python -m unittest discover"
    )


def test_completion_manifest_rejects_stale_workspace_evidence(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan, plan_sha = _plan(tmp_path / "plan.md")
    changed = tmp_path / "src.py"
    changed.write_text("print('old')\n", encoding="utf-8")
    record_terminal_result(
        command="python -m pytest -q",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
        output="1 passed",
    )
    mark_workspace_edited(session_id="s1", cwd=tmp_path, paths=[str(changed)])

    with pytest.raises(CompletionEvidenceError, match="no fresh passing verification"):
        create_plan_completion_manifest(
            session_id="s1",
            cwd=tmp_path,
            objective="implement safely",
            approval_id="approval",
            plan_artifact_path=str(plan),
            plan_artifact_sha256=plan_sha,
            final_response="done",
        )


def test_completion_manifest_rejects_changed_plan_revision(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan, plan_sha = _plan(tmp_path / "plan.md")
    plan.write_text("# Modified plan\n", encoding="utf-8")

    with pytest.raises(CompletionEvidenceError, match="accepted revision"):
        create_plan_completion_manifest(
            session_id="s1",
            cwd=tmp_path,
            objective="implement safely",
            approval_id="approval",
            plan_artifact_path=str(plan),
            plan_artifact_sha256=plan_sha,
            final_response="done",
        )
