from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import plan_mode

    plan_mode._DB_CACHE.clear()
    yield home
    plan_mode._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def session(server):
    sid = "sid-plan-test"
    session_key = "tui-plan-session-1"
    state = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = state
    return sid, session_key, state


def _call(server, method, **params):
    return server._methods[method](1, params)


def test_plan_enter_returns_cache_safe_send(server, session, monkeypatch):
    from hermes_cli import plan_mode

    monkeypatch.setattr(plan_mode, "build_plan_prompt", lambda request, **_: f"PLAN:{request}")
    sid, session_key, _ = session

    response = _call(server, "command.dispatch", name="plan", arg="add feature", session_id=sid)
    result = response["result"]
    assert result["type"] == "send"
    assert result["message"] == "PLAN:add feature"
    assert result["plan_mode"] == "plan"
    assert result["action"] == "enter"
    assert plan_mode.PlanModeManager(session_key).active


def test_plan_approve_returns_execution_kickoff(server, session, monkeypatch):
    from hermes_cli import plan_mode

    monkeypatch.setattr(plan_mode, "build_plan_prompt", lambda request, **_: "PLAN")
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="plan", arg="add feature", session_id=sid)

    response = _call(server, "command.dispatch", name="plan", arg="approve", session_id=sid)
    result = response["result"]
    assert result["type"] == "send"
    assert result["message"] == plan_mode.PLAN_EXECUTION_PROMPT
    assert result["plan_mode"] == "build"
    assert result["action"] == "approve"
    assert not plan_mode.PlanModeManager(session_key).active


def test_busy_session_allows_status_but_not_transition(server, session, monkeypatch):
    from hermes_cli import plan_mode

    monkeypatch.setattr(plan_mode, "build_plan_prompt", lambda request, **_: "PLAN")
    sid, session_key, state = session
    plan_mode.PlanModeManager(session_key).activate("active")
    state["running"] = True

    status = _call(server, "command.dispatch", name="plan", arg="status", session_id=sid)
    assert status["result"]["type"] == "exec"
    assert "PLAN mode is active" in status["result"]["output"]

    approve = _call(server, "command.dispatch", name="plan", arg="approve", session_id=sid)
    assert "error" in approve
    assert approve["error"]["code"] == 4009
    assert plan_mode.PlanModeManager(session_key).active


def test_pending_input_commands_includes_plan(server):
    assert "plan" in server._PENDING_INPUT_COMMANDS
