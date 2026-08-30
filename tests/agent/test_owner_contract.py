from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.owner_contract import (
    CONTRACT_FILE,
    MANIFEST_FILE,
    OwnerContractError,
    active_owner_contract_id,
    harden_tool_guardrail_config,
    load_owner_contract,
    prompt_matches_active_owner_contract,
)
from agent.system_prompt import build_system_prompt_parts
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


def _install(
    home: Path, content: str = "# Owner policy\n\nPERSISTENT CONTRACT\n", skills=None
) -> str:
    payload = content.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    root = home / "owner-contract"
    release = root / "releases" / digest
    release.mkdir(parents=True)
    (release / CONTRACT_FILE).write_bytes(payload)
    manifest = {
        "schema": 1,
        "target": "hermes",
        "host": "kubuntu",
        "id": digest,
        "sha256": digest,
        "bytes": len(payload),
        "required_skills": list(skills or []),
    }
    (release / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    (root / "required").write_text("schema=1\n", encoding="utf-8")
    (root / "current").symlink_to(release)
    return digest


def _agent(**overrides):
    values = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_unconfigured_installation_preserves_upstream_behavior() -> None:
    assert load_owner_contract() is None
    assert active_owner_contract_id() is None
    assert prompt_matches_active_owner_contract("legacy prompt")
    assert harden_tool_guardrail_config({"hard_stop_enabled": False}) == {
        "hard_stop_enabled": False
    }


def test_valid_contract_is_global_and_precedes_project_context(tmp_path: Path) -> None:
    contract_id = _install(get_hermes_home())
    with (
        patch("run_agent.load_soul_md", return_value="SOUL IDENTITY"),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value="PROJECT AGENTS"),
    ):
        parts = build_system_prompt_parts(_agent())

    assert parts["stable"].index("SOUL IDENTITY") < parts["stable"].index(
        "PERSISTENT CONTRACT"
    )
    assert f"Owner-Contract-ID: {contract_id}" in parts["stable"]
    assert "PROJECT AGENTS" not in parts["stable"]
    assert parts["context"] == "PROJECT AGENTS"


def test_required_contract_fails_closed_when_pointer_is_missing(tmp_path: Path) -> None:
    root = get_hermes_home() / "owner-contract"
    root.mkdir()
    (root / "required").write_text("schema=1\n", encoding="utf-8")

    with pytest.raises(OwnerContractError, match="current pointer"):
        load_owner_contract()


def test_content_corruption_fails_closed(tmp_path: Path) -> None:
    home = get_hermes_home()
    contract_id = _install(home)
    contract = home / "owner-contract" / "releases" / contract_id / CONTRACT_FILE
    contract.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(OwnerContractError, match="integrity"):
        load_owner_contract()


def test_required_skill_must_exist_in_active_profile(tmp_path: Path) -> None:
    home = get_hermes_home()
    _install(home, skills=["evidence-gate"])

    with pytest.raises(OwnerContractError, match="evidence-gate"):
        load_owner_contract()

    skill = home / "skills" / "evidence-gate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: evidence-gate\n---\n", encoding="utf-8")
    assert load_owner_contract() is not None


def test_profile_override_selects_the_matching_contract(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_id = _install(first, "first\n")
    second_id = _install(second, "second\n")

    first_token = set_hermes_home_override(first)
    try:
        assert active_owner_contract_id() == first_id
    finally:
        reset_hermes_home_override(first_token)
    second_token = set_hermes_home_override(second)
    try:
        assert active_owner_contract_id() == second_id
    finally:
        reset_hermes_home_override(second_token)


def test_stored_prompt_rebuilds_after_contract_revision_changes(tmp_path: Path) -> None:
    contract_id = _install(get_hermes_home())
    db = MagicMock()
    db.get_session.return_value = {"system_prompt": "legacy prompt without contract id"}
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "session"
    agent.model = "model"
    agent.provider = "provider"
    agent.platform = "cli"
    agent._session_db = db
    agent._build_system_prompt = MagicMock(
        return_value=f"new prompt\nOwner-Contract-ID: {contract_id}\nModel: model\nProvider: provider"
    )

    _restore_or_build_system_prompt(
        agent, None, [{"role": "user", "content": "resume"}]
    )

    agent._build_system_prompt.assert_called_once_with(None)
    db.update_system_prompt.assert_called_once()
    assert f"Owner-Contract-ID: {contract_id}" in agent._cached_system_prompt


def test_active_contract_enforces_bounded_no_progress_hard_stops() -> None:
    _install(get_hermes_home())

    config = harden_tool_guardrail_config({
        "hard_stop_enabled": False,
        "hard_stop_after": {
            "exact_failure": 99,
            "same_tool_failure": 99,
            "idempotent_no_progress": 99,
        },
    })

    assert config["hard_stop_enabled"] is True
    assert config["hard_stop_after"] == {
        "exact_failure": 3,
        "same_tool_failure": 5,
        "idempotent_no_progress": 2,
    }


def test_malformed_guardrail_values_cannot_disable_owner_contract() -> None:
    _install(get_hermes_home())

    config = harden_tool_guardrail_config({
        "warn_after": "not-a-mapping",
        "hard_stop_after": {
            "exact_failure": "not-a-number",
            "same_tool_failure": -10,
            "idempotent_no_progress": True,
        },
    })

    assert config["warnings_enabled"] is True
    assert config["hard_stop_enabled"] is True
    assert config["warn_after"] == {
        "exact_failure": 2,
        "same_tool_failure": 3,
        "idempotent_no_progress": 2,
    }
    assert config["hard_stop_after"] == {
        "exact_failure": 3,
        "same_tool_failure": 5,
        "idempotent_no_progress": 2,
    }
