from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from tools import approval


def _activate_contract(home: Path) -> None:
    content = b"# owner contract\n"
    digest = hashlib.sha256(content).hexdigest()
    root = home / "owner-contract"
    release = root / "releases" / digest
    release.mkdir(parents=True)
    (release / "OWNER_CONTRACT.md").write_bytes(content)
    (release / "manifest.json").write_text(
        json.dumps({
            "schema": 1,
            "target": "hermes",
            "host": "kubuntu",
            "id": digest,
            "sha256": digest,
            "bytes": len(content),
            "required_skills": [],
        }),
        encoding="utf-8",
    )
    (root / "required").write_text("schema=1\n", encoding="utf-8")
    (root / "current").symlink_to(release)


@pytest.mark.parametrize(
    ("command", "high_impact"),
    [
        ("git status --short", False),
        ("git commit -m safe", False),
        ("systemctl --user restart one-scoped-service.service", False),
        ("rm -rf /tmp/task-artifacts", True),
        ("git reset --hard HEAD~1", True),
        ("printf x > .env.production", True),
        ("alembic upgrade head", True),
        ("python manage.py migrate", True),
        ("tool backfill --production --apply", True),
    ],
)
def test_owner_contract_classifies_only_high_impact_actions(
    command: str, high_impact: bool
) -> None:
    _activate_contract(get_hermes_home())
    detected, _description = approval.detect_owner_high_impact_command(command)
    assert detected is high_impact


def test_yolo_does_not_bypass_owner_high_impact_gate_without_a_human() -> None:
    _activate_contract(get_hermes_home())
    token = approval.set_current_session_key("owner-session")
    approval.enable_session_yolo("owner-session")
    try:
        result = approval.check_all_command_guards("alembic upgrade head", "local")
    finally:
        approval.disable_session_yolo("owner-session")
        approval.reset_current_session_key(token)

    assert result["approved"] is False
    assert "origin-bound owner decision" in result["message"]


def test_yolo_still_allows_ordinary_coding_commands() -> None:
    _activate_contract(get_hermes_home())
    token = approval.set_current_session_key("owner-session")
    approval.enable_session_yolo("owner-session")
    try:
        result = approval.check_all_command_guards(
            "git add src/app.py && git commit -m scoped-change",
            "local",
        )
    finally:
        approval.disable_session_yolo("owner-session")
        approval.reset_current_session_key(token)

    assert result["approved"] is True
