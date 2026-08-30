"""Bounded evidence manifest for an approved-plan completion claim."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.owner_contract import active_owner_contract_id
from agent.verification_evidence import verification_status
from hermes_constants import get_hermes_home


SCHEMA = 1


class CompletionEvidenceError(RuntimeError):
    """The completion claim cannot be backed by fresh evidence."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *command],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_facts(cwd: Path) -> dict[str, Any]:
    root = _git(["rev-parse", "--show-toplevel"], cwd)
    if not root:
        return {"repository": False}
    root_path = Path(root).resolve()
    status = _git(["status", "--porcelain=v1", "--untracked-files=all"], root_path)
    return {
        "repository": True,
        "root": str(root_path),
        "head": _git(["rev-parse", "HEAD"], root_path),
        "branch": _git(["branch", "--show-current"], root_path),
        "dirty": bool(status),
        "status_sha256": _sha256_text(status),
        "status_entry_count": len(status.splitlines()) if status else 0,
    }


def _verify_plan_artifact(path: str, expected_sha256: str, cwd: Path) -> dict[str, Any]:
    if not path:
        raise CompletionEvidenceError("approved plan has no saved artifact path")
    artifact = Path(path).expanduser()
    if not artifact.is_absolute():
        artifact = cwd / artifact
    try:
        content = (
            artifact
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
    except OSError as exc:
        raise CompletionEvidenceError("approved plan artifact cannot be read") from exc
    digest = _sha256_text(content)
    if not expected_sha256 or digest != expected_sha256:
        raise CompletionEvidenceError(
            "approved plan artifact no longer matches its accepted revision"
        )
    return {"path": str(artifact.resolve()), "sha256": digest}


def create_plan_completion_manifest(
    *,
    session_id: str,
    cwd: str | Path,
    objective: str,
    approval_id: str,
    plan_artifact_path: str,
    plan_artifact_sha256: str,
    final_response: str,
) -> tuple[Path, str]:
    """Validate and atomically persist the latest completion evidence.

    The manifest stores hashes and bounded summaries, never the raw objective,
    response, command output, or secret-bearing environment.
    """
    workspace = Path(cwd).expanduser().resolve()
    verification = verification_status(session_id=session_id, cwd=workspace)
    changed_paths = sorted({
        str(path) for path in verification.get("changed_paths") or []
    })[-200:]
    verification_state = str(verification.get("status") or "unverified")
    if changed_paths and verification_state != "passed":
        raise CompletionEvidenceError(
            f"workspace edits have no fresh passing verification (status={verification_state})"
        )
    plan = _verify_plan_artifact(plan_artifact_path, plan_artifact_sha256, workspace)
    evidence = verification.get("evidence")
    check = None
    if isinstance(evidence, dict):
        check = {
            key: evidence.get(key)
            for key in (
                "created_at",
                "canonical_command",
                "kind",
                "scope",
                "status",
                "exit_code",
                "root",
                "output_summary",
            )
        }
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "approved_plan_completion",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id_sha256": _sha256_text(session_id),
        "objective_sha256": _sha256_text(objective),
        "approval_id_sha256": _sha256_text(approval_id),
        "owner_contract_id": active_owner_contract_id(),
        "plan": plan,
        "workspace": _git_facts(workspace),
        "changed_artifacts": changed_paths,
        "verification": {"status": verification_state, "check": check},
        "final_response_sha256": _sha256_text(final_response),
        "completion_status": "complete",
    }
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    manifest_sha256 = _sha256_text(canonical)
    manifest["manifest_sha256"] = manifest_sha256

    safe_session = _sha256_text(session_id)[:24]
    directory = get_hermes_home() / "evidence-manifests"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / f"{safe_session}.json"
    temporary = directory / f".{safe_session}.tmp-{os.getpid()}"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return target, manifest_sha256


def verify_completion_manifest(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompletionEvidenceError(
            "completion evidence manifest cannot be read"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise CompletionEvidenceError("completion evidence manifest shape is invalid")
    expected = manifest.get("manifest_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise CompletionEvidenceError("completion evidence manifest hash is invalid")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if _sha256_text(canonical) != expected:
        raise CompletionEvidenceError(
            "completion evidence manifest integrity check failed"
        )
    return manifest
