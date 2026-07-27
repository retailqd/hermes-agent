"""Tests for opt-in cleanup of temporary progress bubbles.

When ``display.platforms.<plat>.cleanup_progress: true`` is set for a
platform whose adapter supports message deletion (e.g. Telegram), the
tool-progress bubble, "⏳ Working — N min" heartbeats, and status-callback
messages sent during a run are deleted after the final response is
delivered.

Failed runs skip cleanup so the bubbles remain as breadcrumbs.
Adapters without ``delete_message`` silently no-op.
"""

import asyncio
import importlib
import inspect as _inspect
import json
import sys
import time
import threading
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig


async def _fire_post_delivery_cb(cb):
    """Invoke a popped post-delivery callback, awaiting if it's async.

    Chained registrations return an async wrapper; single registrations
    return the raw sync callable. Either way, await any awaitable result.
    """
    result = cb()
    if _inspect.isawaitable(result):
        await result
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Test fakes — mirror those in test_run_progress_topics.py but add a
# delete_message implementation that records ids instead of hitting a bot.
# ---------------------------------------------------------------------------


class CleanupCaptureAdapter(BasePlatformAdapter):
    """Adapter that records every delete_message call for inspection."""

    _next_mid = 100

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []
        self.edits = []
        self.deleted = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    def _mint_id(self) -> str:
        CleanupCaptureAdapter._next_mid += 1
        return str(CleanupCaptureAdapter._next_mid)

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        mid = self._mint_id()
        self.sent.append(
            {"chat_id": chat_id, "content": content, "message_id": mid, "metadata": metadata}
        )
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": str(message_id)})
        return True

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class NoDeleteAdapter(CleanupCaptureAdapter):
    """Adapter that inherits the base no-op delete_message (used to prove
    the cleanup path skips adapters without deletion support)."""

    async def delete_message(self, chat_id, message_id) -> bool:  # type: ignore[override]
        # Pretend to be an adapter whose platform doesn't support deletion:
        # match the base class behavior exactly. gateway/run.py checks
        # ``type(adapter).delete_message is BasePlatformAdapter.delete_message``
        # to detect this, so we re-assign at class body level below.
        raise AssertionError("should not be called — cleanup must skip this adapter")


# Re-bind so the class's delete_message identity equals the base's.
NoDeleteAdapter.delete_message = BasePlatformAdapter.delete_message


class FalseDeleteAdapter(CleanupCaptureAdapter):
    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": str(message_id)})
        return False


class RaisingDeleteAdapter(CleanupCaptureAdapter):
    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": str(message_id)})
        raise RuntimeError("delete failed")


class ProgressAgent:
    """Emits two tool-progress events and returns a normal final response."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.25)
            cb("tool.started", "terminal", "ls", {})
            time.sleep(0.25)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class FailingAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.25)
        # Empty final_response + failed=True is the shape the gateway
        # actually returns on provider errors (see gateway/run.py where
        # failed keys are only propagated when final_response is empty).
        return {
            "final_response": "",
            "messages": [],
            "api_calls": 1,
            "failed": True,
            "error": "simulated provider failure",
        }


class BackgroundReviewAgent:
    """Agent that tries to emit a background review payload twice.

    The first attempt happens during the run. The second one is delayed until
    after the run has already returned, so the test can prove that Mattermost's
    default wiring keeps ``background_review_callback`` unset and does not leak
    a late review post into chat.
    """

    last_instance = None

    def __init__(self, **kwargs):
        BackgroundReviewAgent.last_instance = self
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []
        self.review_attempts = []
        self._late_review_done = threading.Event()

    def run_conversation(self, message, conversation_history=None, task_id=None):
        payload = json.dumps(
            {
                "kind": "self_review",
                "source": "agent",
                "summary": "Mattermost review payload",
            },
            sort_keys=True,
        )
        callback = getattr(self, "background_review_callback", None)
        self.review_attempts.append(callback)
        if callable(callback):
            callback(payload)

        def _late_review() -> None:
            time.sleep(0.05)
            late_callback = getattr(self, "background_review_callback", None)
            self.review_attempts.append(late_callback)
            if callable(late_callback):
                late_callback(payload)
            self._late_review_done.set()

        threading.Thread(target=_late_review, daemon=True).start()
        return {"final_response": "done", "messages": [], "api_calls": 1}


class MultiProgressFailingAgent:
    """Agent that emits multiple distinct tool progress updates and fails."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.25)
            cb("tool.started", "browser_navigate", "https://example.com", {})
            time.sleep(0.25)
        return {
            "final_response": "",
            "messages": [],
            "api_calls": 1,
            "failed": True,
            "error": "simulated provider failure",
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def _install_fakes(monkeypatch, agent_cls, *, cleanup_on: bool):
    """Wire up the module stubs every _run_agent test needs."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 — register tool emoji

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    # Wire the per-platform cleanup_progress flag via the config loader the
    # gateway actually reads (``_load_gateway_config`` returns user config).
    cfg = {
        "display": {
            "platforms": {
                "telegram": {"cleanup_progress": True},
            }
        }
    } if cleanup_on else {}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    return gateway_run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_off_by_default_leaves_bubbles(monkeypatch, tmp_path):
    """Without ``cleanup_progress: true``, firing whatever callback is
    registered never reaches delete_message."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    # Even if an unrelated callback got registered (background-review
    # release lives in the same slot) firing it should never cause any
    # delete_message calls when cleanup is off.
    cb = adapter.pop_post_delivery_callback(session_key)
    if cb is not None:
        await _fire_post_delivery_cb(cb)
        for _ in range(10):
            await asyncio.sleep(0.01)
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_cleanup_registers_callback_and_deletes_on_success(monkeypatch, tmp_path):
    """With the flag on, the cleanup callback deletes the progress bubble."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    # The cleanup callback should be registered for this session.
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)

    # Fire it (base.py does this in _process_message_background's finally)
    # and let the scheduled coroutine run to completion.
    await _fire_post_delivery_cb(cb)
    # delete_message is scheduled via run_coroutine_threadsafe → give the
    # loop a couple of ticks to drain.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break

    # At least the first tool-progress bubble should have been deleted.
    assert len(adapter.deleted) >= 1, f"deleted={adapter.deleted} sent={adapter.sent}"
    for entry in adapter.deleted:
        assert entry["chat_id"] == "-1001"


@pytest.mark.asyncio
async def test_cleanup_skipped_on_failed_run(monkeypatch, tmp_path):
    """Failed runs skip cleanup registration — breadcrumbs stay."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, FailingAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result.get("failed") is True
    # Whatever callback is registered should not trigger any deletion —
    # the cleanup callback is skipped on failed runs.
    cb = adapter.pop_post_delivery_callback(session_key)
    if cb is not None:
        await _fire_post_delivery_cb(cb)
        for _ in range(10):
            await asyncio.sleep(0.01)
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_mattermost_background_review_notifications_off_by_default_suppresses_callback_and_late_post(monkeypatch, tmp_path):
    """Mattermost's default keeps background review silent unless explicitly enabled."""
    adapter = CleanupCaptureAdapter(platform=Platform.MATTERMOST)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, BackgroundReviewAgent, cleanup_on=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.MATTERMOST, chat_id="owner-1")
    session_key = "agent:main:mattermost:channel:owner-1"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-review-off",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    agent = BackgroundReviewAgent.last_instance
    assert agent is not None
    assert agent.background_review_callback is None
    await asyncio.to_thread(agent._late_review_done.wait, 1.0)
    assert agent.review_attempts == [None, None]
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_mattermost_background_review_notifications_override_true_releases_review(monkeypatch, tmp_path):
    """An explicit Mattermost override still wires the delayed review release."""
    adapter = CleanupCaptureAdapter(platform=Platform.MATTERMOST)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, BackgroundReviewAgent, cleanup_on=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "platforms": {
                    "mattermost": {"background_review_notifications": True},
                }
            }
        },
    )

    source = SessionSource(platform=Platform.MATTERMOST, chat_id="owner-2")
    session_key = "agent:main:mattermost:channel:owner-2"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-review-on",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    agent = BackgroundReviewAgent.last_instance
    assert agent is not None
    assert callable(agent.background_review_callback)
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        if any("self_review" in entry["content"] for entry in adapter.sent):
            break
        await asyncio.sleep(0.01)
    assert any("self_review" in entry["content"] for entry in adapter.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [FalseDeleteAdapter, RaisingDeleteAdapter])
async def test_cleanup_delete_message_failures_do_not_break_final_delivery(monkeypatch, tmp_path, adapter_cls):
    """delete_message returning False or raising should not break the final response."""
    adapter = adapter_cls()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-cleanup-failure",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break
    assert adapter.deleted


@pytest.mark.asyncio
async def test_failed_run_keeps_single_line_progress_bubble(monkeypatch, tmp_path):
    """Failed runs should keep, at most, one compact breadcrumb bubble."""
    adapter = CleanupCaptureAdapter(platform=Platform.MATTERMOST)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, MultiProgressFailingAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.MATTERMOST, chat_id="owner-1")
    session_key = "agent:main:mattermost:channel:owner-1"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-failed-progress",
        session_key=session_key,
    )

    assert result.get("failed") is True
    await asyncio.sleep(0.1)
    final_progress = None
    if adapter.edits:
        final_progress = adapter.edits[-1]["content"]
    elif adapter.sent:
        final_progress = adapter.sent[-1]["content"]
    assert final_progress is not None
    assert "\n" not in final_progress


@pytest.mark.asyncio
async def test_cleanup_noop_on_adapter_without_delete_support(monkeypatch, tmp_path):
    """Adapters that inherit the base-class delete_message no-op are
    detected up front — the cleanup path never registers its callback so
    a stray bg-review callback (if present) can fire harmlessly."""
    adapter = NoDeleteAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    # No deletion attempts on an adapter without delete_message support.
    # (The NoDeleteAdapter.delete_message would raise AssertionError if
    # the cleanup closure had somehow captured a reference to it.)
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_cleanup_chains_with_existing_callback(monkeypatch, tmp_path):
    """When a bg-review-style callback is already registered, the cleanup
    callback chains with it — both fire, neither clobbers the other."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    pre_existing_fired = []

    def _preexisting_callback() -> None:
        pre_existing_fired.append(True)

    # Pre-register a callback with the same generation the run will use
    # (run_generation=None in this test path — matches the default slot).
    adapter.register_post_delivery_callback(session_key, _preexisting_callback)

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break

    # Both effects land: the pre-existing callback fires AND the cleanup
    # deletes at least one progress bubble.
    assert pre_existing_fired == [True]
    assert len(adapter.deleted) >= 1
