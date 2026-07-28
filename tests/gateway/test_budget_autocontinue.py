"""Gateway regressions for automatic continuation after iteration-budget exits."""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _SequentialAgent:
    outcomes: list[dict] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        type(self).calls.append(
            {
                "user_message": user_message,
                "history": list(conversation_history or []),
                "synthetic_turn": bool(
                    getattr(self, "_budget_autocontinuation_turn", False)
                ),
                "handoff_enabled": bool(
                    getattr(self, "_gateway_budget_autocontinue_enabled", False)
                ),
            }
        )
        outcome = dict(type(self).outcomes.pop(0))
        history = list(conversation_history or [])
        user = {"role": "user", "content": user_message}
        if getattr(self, "_budget_autocontinuation_turn", False):
            user["_budget_continuation_synthetic"] = True
        assistant = {
            "role": "assistant",
            "content": outcome.get("final_response", ""),
        }
        if (
            outcome.get("turn_exit_reason") == "budget_exhausted"
            and getattr(self, "_gateway_budget_autocontinue_enabled", False)
        ):
            assistant["_budget_continuation_synthetic"] = True
        outcome["messages"] = history + [user, assistant]
        outcome.setdefault("api_calls", 300)
        outcome.setdefault("completed", False)
        outcome.setdefault("failed", False)
        return outcome


class _TimeoutAgent(_SequentialAgent):
    timeout_calls: list[str] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._release = threading.Event()

    def get_activity_summary(self):
        return {
            "seconds_since_activity": 999.0,
            "last_activity_desc": "test-block",
            "api_call_count": len(type(self).timeout_calls),
            "max_iterations": 300,
        }

    def interrupt(self, message=None):
        self._release.set()

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        call_index = len(type(self).timeout_calls)
        type(self).timeout_calls.append(user_message)
        history = list(conversation_history or [])
        if call_index == 0:
            self._release.wait(timeout=10)
            return {
                "final_response": "interrupted for timeout",
                "messages": history
                + [{"role": "assistant", "content": "interrupted for timeout"}],
                "api_calls": 1,
                "completed": False,
                "interrupted": True,
                "turn_exit_reason": "interrupted",
                "failed": False,
            }
        return {
            "final_response": "recovered after real timeout",
            "messages": history
            + [{"role": "assistant", "content": "recovered after real timeout"}],
            "api_calls": 1,
            "completed": True,
            "interrupted": False,
            "turn_exit_reason": "text_response(stop)",
            "failed": False,
        }


def _install_agent(monkeypatch, outcomes: list[dict]) -> None:
    _SequentialAgent.outcomes = [dict(item) for item in outcomes]
    _SequentialAgent.calls = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _SequentialAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _install_timeout_agent(monkeypatch) -> None:
    _TimeoutAgent.timeout_calls = []
    fake_run_agent = types.ModuleType("run_agent")
    setattr(fake_run_agent, "AIAgent", _TimeoutAgent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _runner() -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner._session_run_generation = {}
    runner._draining = False
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="owner",
    )


def _configure_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"core"},
    )


@pytest.mark.asyncio
async def test_budget_exhaustion_continues_three_times_then_surfaces_cap(
    monkeypatch, tmp_path
):
    _install_agent(
        monkeypatch,
        [
            {
                "final_response": f"handoff-{index}",
                "turn_exit_reason": "budget_exhausted",
            }
            for index in range(4)
        ],
    )
    _configure_runtime(monkeypatch, tmp_path)
    runner = _runner()

    result = await runner._run_agent(
        message="finish the task",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert len(_SequentialAgent.calls) == 4
    assert _SequentialAgent.calls[0]["user_message"] == "finish the task"
    assert [call["synthetic_turn"] for call in _SequentialAgent.calls] == [
        False,
        True,
        True,
        True,
    ]
    assert all(
        call["user_message"] == runner._BUDGET_CONTINUATION_PROMPT
        for call in _SequentialAgent.calls[1:]
    )
    assert result["turn_exit_reason"] == "budget_exhausted"
    assert result["budget_autocontinue_exhausted"] is True
    assert result["budget_continuation_rounds"] == 3
    assert result["final_response"] == "handoff-3"


@pytest.mark.asyncio
async def test_pending_steer_wins_over_budget_autocontinuation(monkeypatch, tmp_path):
    _install_agent(
        monkeypatch,
        [
            {
                "final_response": "internal handoff",
                "turn_exit_reason": "budget_exhausted",
                "pending_steer": "owner correction",
            },
            {
                "final_response": "corrected result",
                "turn_exit_reason": "text_response(stop)",
                "completed": True,
                "api_calls": 1,
            },
        ],
    )
    _configure_runtime(monkeypatch, tmp_path)
    runner = _runner()

    result = await runner._run_agent(
        message="finish the task",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert [call["user_message"] for call in _SequentialAgent.calls] == [
        "finish the task",
        "owner correction",
    ]
    assert _SequentialAgent.calls[1]["synthetic_turn"] is False
    assert result["final_response"] == "corrected result"
    assert result.get("budget_continuation_rounds") is None


@pytest.mark.asyncio
async def test_clean_timeout_exit_uses_timeout_continuation_prompt(monkeypatch, tmp_path):
    _install_agent(
        monkeypatch,
        [
            {
                "final_response": "timeout handoff",
                "turn_exit_reason": "timeout",
            },
            {
                "final_response": "recovered result",
                "turn_exit_reason": "text_response(stop)",
                "completed": True,
                "api_calls": 1,
            },
        ],
    )
    _configure_runtime(monkeypatch, tmp_path)
    runner = _runner()

    result = await runner._run_agent(
        message="finish the task",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert [call["user_message"] for call in _SequentialAgent.calls] == [
        "finish the task",
        runner._TIMEOUT_CONTINUATION_PROMPT,
    ]
    assert result["final_response"] == "recovered result"
    assert result["budget_continuation_rounds"] == 1


@pytest.mark.asyncio
async def test_real_inactivity_timeout_requires_clean_unwind_then_continues(
    monkeypatch, tmp_path
):
    _install_timeout_agent(monkeypatch)
    _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0.01")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT_WARNING", "0")
    runner = _runner()
    runner._TIMEOUT_UNWIND_GRACE_SECONDS = 1.0

    result = await runner._run_agent(
        message="finish after timeout",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert _TimeoutAgent.timeout_calls == [
        "finish after timeout",
        runner._TIMEOUT_CONTINUATION_PROMPT,
    ]
    assert result["final_response"] == "recovered after real timeout"
    assert result["budget_continuation_rounds"] == 1
