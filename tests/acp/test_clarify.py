"""Tests for ACP clarification selection bridging."""

import asyncio
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from acp_adapter.clarify import make_acp_clarify_callback
from tools.clarify_tool import clarify_tool


def _callback_for(outcome):
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    request_permission = AsyncMock(name="request_permission")
    future = MagicMock(spec=Future)
    future.result.return_value = RequestPermissionResponse(outcome=outcome)
    scheduled = {}

    def _schedule(coro, passed_loop):
        scheduled["coro"] = coro
        scheduled["loop"] = passed_loop
        return future

    callback = make_acp_clarify_callback(request_permission, loop, "s1")
    return callback, request_permission, scheduled, future, loop, _schedule


def test_selected_choice_round_trips_exact_label():
    callback, request_permission, scheduled, _, loop, schedule = _callback_for(
        AllowedOutcome(option_id="choice_1", outcome="selected")
    )
    with patch("agent.async_utils.asyncio.run_coroutine_threadsafe", side_effect=schedule):
        result = callback("Which target?", ["Staging", "Production"])

    scheduled["coro"].close()
    _, kwargs = request_permission.call_args
    assert result == "Production"
    assert scheduled["loop"] is loop
    assert kwargs["session_id"] == "s1"
    assert [option.name for option in kwargs["options"]] == [
        "Staging",
        "Production",
        "Cancel",
    ]
    assert kwargs["tool_call"].kind == "think"


def test_clarify_tool_round_trips_acp_selection_as_json():
    callback, _, scheduled, _, _, schedule = _callback_for(
        AllowedOutcome(option_id="choice_0", outcome="selected")
    )
    with patch("agent.async_utils.asyncio.run_coroutine_threadsafe", side_effect=schedule):
        payload = clarify_tool("Which path?", ["Safe", "Fast"], callback=callback)
    scheduled["coro"].close()

    import json

    assert json.loads(payload) == {
        "question": "Which path?",
        "choices_offered": ["Safe", "Fast"],
        "user_response": "Safe",
    }


def test_cancel_and_open_ended_fail_closed():
    callback, _, scheduled, _, _, schedule = _callback_for(
        DeniedOutcome(outcome="cancelled")
    )
    with patch("agent.async_utils.asyncio.run_coroutine_threadsafe", side_effect=schedule):
        with pytest.raises(RuntimeError, match="cancelled"):
            callback("Proceed?", ["Yes", "No"])
    scheduled["coro"].close()

    with pytest.raises(RuntimeError, match="selectable"):
        callback("Type anything", None)


def test_timeout_cancels_future_and_fails_closed():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    request_permission = AsyncMock(name="request_permission")
    future = MagicMock(spec=Future)
    future.result.side_effect = TimeoutError("timed out")
    scheduled = {}

    def _schedule(coro, passed_loop):
        scheduled["coro"] = coro
        return future

    callback = make_acp_clarify_callback(request_permission, loop, "s1", timeout=0.01)
    with patch("agent.async_utils.asyncio.run_coroutine_threadsafe", side_effect=_schedule):
        with pytest.raises(RuntimeError, match="timed out"):
            callback("Choose", ["A", "B"])
    scheduled["coro"].close()
    future.cancel.assert_called_once()
