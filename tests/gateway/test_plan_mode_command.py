from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import plan_mode


class _SessionEntry:
    session_id = "sid-matrix-plan"


class _SessionStore:
    def get_or_create_session(self, _source):
        return _SessionEntry()


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    plan_mode._DB_CACHE.clear()

    instance = object.__new__(GatewayRunner)
    instance.config = GatewayConfig(
        platforms={Platform.MATRIX: PlatformConfig(enabled=True, token="token")}
    )
    instance.session_store = _SessionStore()
    instance.adapters = {}
    yield instance
    plan_mode._DB_CACHE.clear()


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.MATRIX,
            chat_id="!room:example.org",
            chat_type="room",
            user_id="@owner:example.org",
        ),
        message_id="matrix-plan-message",
    )


@pytest.mark.asyncio
async def test_matrix_plan_command_binds_native_state_to_conversation(runner, monkeypatch):
    monkeypatch.setattr(plan_mode, "build_plan_prompt", lambda request, **_: f"PLAN:{request}")

    entered = await GatewayRunner._handle_plan_command(runner, _event("/plan implement it"))
    assert entered.plan_mode == "plan"
    assert entered.prompt == "PLAN:implement it"
    assert plan_mode.PlanModeManager(_SessionEntry.session_id).active

    status = await GatewayRunner._handle_plan_command(runner, _event("/plan status"))
    assert status.action == "status"
    assert "Mutating tools are blocked" in status.message

    approved = await GatewayRunner._handle_plan_command(runner, _event("/plan approve"))
    assert approved.plan_mode == "plan"
    state = plan_mode.PlanModeManager(_SessionEntry.session_id).state
    assert approved.prompt == plan_mode.build_plan_execution_prompt(state.approval_id)
    assert state.build_pending


def test_gateway_clarify_failures_cannot_become_owner_answers():
    source = inspect.getsource(GatewayRunner._run_agent_inner)
    assert "[clarify prompt could not be delivered]" not in source
    assert "[user did not respond within" not in source
    assert 'if not send_ok:' in source
    assert 'return ""' in source
