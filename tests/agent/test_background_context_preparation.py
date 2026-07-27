import copy
import threading
import time
from typing import Any

import pytest

from agent.context_compressor import (
    ContextCompressor,
    BACKGROUND_TAIL_MODE_METADATA_KEY,
)


def _compressor() -> ContextCompressor:
    compressor = ContextCompressor(
        model="test-model",
        config_context_length=200_000,
        protect_first_n=1,
        protect_last_n=2,
        quiet_mode=True,
    )
    compressor.threshold_tokens = 100
    compressor.tail_token_budget = 50
    compressor._session_id = "session-a"
    return compressor


def _messages(count: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index} " + ("x" * 300),
        }
        for index in range(count)
    ]


def test_background_preparation_is_non_blocking():
    compressor = _compressor()
    release = threading.Event()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            release.wait(2)
            return "prepared summary"

    setattr(compressor, "_clone_for_background", lambda: Worker())

    started_at = time.monotonic()
    assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert time.monotonic() - started_at < 0.2

    release.set()
    assert compressor.wait_for_background_preparation(timeout=2)
    assert compressor._background_candidate is not None
    assert compressor._background_candidate["summary"] == "prepared summary"


def test_background_preparation_starts_at_half_hard_threshold(monkeypatch):
    compressor = _compressor()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            return "prepared summary"

    monkeypatch.setattr(
        "agent.context_compressor.estimate_messages_tokens_rough",
        lambda messages: 0,
    )
    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())

    assert compressor.maybe_prepare_background(_messages(), current_tokens=50)
    assert compressor.wait_for_background_preparation(timeout=1)


def test_background_preparation_uses_live_estimate_when_reported_tokens_are_stale(monkeypatch):
    compressor = _compressor()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            return "prepared summary"

    monkeypatch.setattr(
        "agent.context_compressor.estimate_messages_tokens_rough",
        lambda messages: 80,
    )
    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())

    assert compressor.maybe_prepare_background(_messages(), current_tokens=1)
    assert compressor.wait_for_background_preparation(timeout=1)


def test_published_candidate_is_immutable_and_does_not_retain_source_turns(monkeypatch):
    compressor = _compressor()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            return "prepared summary"

    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())

    assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert compressor.wait_for_background_preparation(timeout=1)
    candidate = compressor._background_candidate
    assert candidate is not None
    assert "turns" not in candidate
    with pytest.raises(TypeError):
        candidate["summary"] = "mutated"


def test_policy_fingerprint_changes_with_summary_and_partition_semantics():
    compressor = _compressor()
    original = compressor._background_policy_fingerprint()

    compressor._CONTENT_MAX += 1

    assert compressor._background_policy_fingerprint() != original


def test_exact_background_candidate_is_consumed_without_new_llm_call():
    compressor = _compressor()
    turns = _messages(4)
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": compressor._background_source_hash(turns),
        "count": len(turns),
        "focus_topic": "topic",
        "summary": "prepared summary",
    }
    setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("foreground summarizer should not run")
        ),
    )

    assert compressor._consume_background_candidate(turns, "topic") == "prepared summary"


def test_compress_exact_ready_candidate_avoids_second_call_and_preserves_boundaries(
    monkeypatch,
):
    compressor = _compressor()
    messages = _messages()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            return "prepared exact summary"

    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())
    assert compressor.maybe_prepare_background(messages, current_tokens=80)
    assert compressor.wait_for_background_preparation(1)
    monkeypatch.setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ready candidate started a second summarizer call")
        ),
    )

    result = compressor.compress(messages, current_tokens=200)

    assert result[0] == messages[0]
    assert result[-2:] == messages[-2:]
    assert any("prepared exact summary" in str(item.get("content")) for item in result)


def test_manual_focus_rejects_automatic_background_candidate():
    compressor = _compressor()
    turns = _messages(4)
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": compressor._background_source_hash(turns),
        "count": len(turns),
        "focus_topic": "automatic focus",
        "requested_focus_topic": None,
        "summary": "automatic summary",
    }

    assert compressor._consume_background_candidate(
        turns,
        "manual focus",
        requested_focus_topic="manual focus",
    ) is None

    compressor._background_candidate["requested_focus_topic"] = "manual focus"
    assert compressor._consume_background_candidate(
        turns,
        "manual focus",
        requested_focus_topic="manual focus",
    ) == "automatic summary"


def test_background_candidate_summarizes_only_appended_tail():
    compressor = _compressor()
    turns = _messages(5)
    covered = turns[:3]
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": compressor._background_source_hash(covered),
        "count": len(covered),
        "focus_topic": "topic",
        "summary": "prepared summary",
    }
    captured = []

    def summarize_tail(messages, focus_topic=None):
        captured.extend(messages)
        assert compressor._previous_summary == "prepared summary"
        return "combined summary"

    setattr(compressor, "_generate_summary", summarize_tail)

    assert compressor._consume_background_candidate(turns, "topic") == "combined summary"
    assert captured == turns[3:]


def test_compress_append_after_watermark_uses_literal_delta_when_it_fits(monkeypatch):
    compressor = _compressor()
    compressor.threshold_tokens = 1000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "head user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-head",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-head", "content": "head tool result"},
        {"role": "assistant", "content": "prepared prefix assistant"},
        {"role": "user", "content": "prepared prefix user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-literal",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-literal", "content": "literal tool result"},
        {"role": "user", "content": "literal follow-up user"},
        {"role": "assistant", "content": "tail assistant"},
        {"role": "user", "content": "tail user"},
    ]
    prepared_count = 2
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": compressor._background_source_hash(messages[4:4 + prepared_count]),
        "count": prepared_count,
        "focus_topic": "topic",
        "requested_focus_topic": None,
        "summary": "prepared prefix summary",
    }

    monkeypatch.setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("literal delta path should not re-summarize the tail")
        ),
    )
    monkeypatch.setattr(compressor, "_protect_head_size", lambda _: 4)
    monkeypatch.setattr(compressor, "_align_boundary_forward", lambda _messages, idx: idx)
    monkeypatch.setattr(compressor, "_find_tail_cut_by_tokens", lambda _messages, head_end, token_budget=None: 9)

    result = compressor.compress(messages, current_tokens=200)

    summary_msg = next(m for m in result if m.get("_compressed_summary"))
    assert summary_msg[BACKGROUND_TAIL_MODE_METADATA_KEY] == "prepared-prefix-literal-delta"
    assert result[0]["role"] == "system"
    assert result[1:4] == messages[1:4]
    assert result[5:8] == messages[6:9]
    assert result[8:10] == messages[9:11]
    assert result[5]["tool_calls"][0]["id"] == "call-literal"
    assert result[6]["tool_call_id"] == "call-literal"


def test_ready_prefix_bypasses_newer_inflight_worker(monkeypatch):
    compressor = _compressor()
    compressor.threshold_tokens = 5000
    messages = _messages()
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    calls = []

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            calls.append(copy.deepcopy(turns))
            if len(calls) == 1:
                return "older ready prefix"
            refresh_started.set()
            release_refresh.wait(2)
            return "newer refresh"

    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())
    assert compressor.maybe_prepare_background(messages, current_tokens=4000)
    assert compressor.wait_for_background_preparation(1)

    appended = messages + [
        {"role": "user", "content": "small append-only delta"},
        {"role": "assistant", "content": "small append-only answer"},
    ]
    assert compressor.maybe_prepare_background(appended, current_tokens=4000)
    assert refresh_started.wait(1)
    monkeypatch.setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ready-prefix path started a foreground summary")
        ),
    )

    started_at = time.monotonic()
    result = compressor.compress(appended, current_tokens=4000)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert compressor._last_compress_aborted is False
    assert any("older ready prefix" in str(m.get("content")) for m in result)
    assert result[-2:] == appended[-2:]
    release_refresh.set()


def test_compress_append_after_watermark_rejects_oversized_literal_delta_and_summarizes_tail(monkeypatch):
    compressor = _compressor()
    compressor.threshold_tokens = 250
    long_literal = "literal delta " * 120
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "head user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-head",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-head", "content": "head tool result"},
        {"role": "assistant", "content": "prepared prefix assistant"},
        {"role": "user", "content": "prepared prefix user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-literal",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-literal", "content": long_literal},
        {"role": "assistant", "content": "tail assistant"},
        {"role": "user", "content": "tail user"},
    ]
    prepared_count = 2
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": compressor._background_source_hash(messages[4:4 + prepared_count]),
        "count": prepared_count,
        "focus_topic": "topic",
        "requested_focus_topic": None,
        "summary": "prepared prefix summary",
    }
    captured = []

    def summarize_tail(turns, focus_topic=None):
        captured.append(copy.deepcopy(turns))
        return "prepared-prefix summarized-delta"

    monkeypatch.setattr(compressor, "_generate_summary", summarize_tail)
    monkeypatch.setattr(compressor, "_protect_head_size", lambda _: 4)
    monkeypatch.setattr(compressor, "_align_boundary_forward", lambda _messages, idx: idx)
    monkeypatch.setattr(compressor, "_find_tail_cut_by_tokens", lambda _messages, head_end, token_budget=None: 10)

    result = compressor.compress(messages, current_tokens=200)

    assert captured == [messages[6:10]]
    summary_msg = next(m for m in result if m.get("_compressed_summary"))
    assert summary_msg[BACKGROUND_TAIL_MODE_METADATA_KEY] == "prepared-prefix-summarized-delta"
    assert any(long_literal in str(m.get("content")) for m in result) is False


def test_concurrent_hard_gates_coalesce_identical_candidate_consumption(monkeypatch):
    compressor = _compressor()
    messages = _messages()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            return "prepared concurrent prefix"

    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())
    assert compressor.maybe_prepare_background(messages, current_tokens=80)
    assert compressor.wait_for_background_preparation(1)

    appended = messages + [
        {"role": "user", "content": "concurrent append user"},
        {"role": "assistant", "content": "concurrent append assistant"},
    ]
    summary_started = threading.Event()
    release_summary = threading.Event()
    summary_calls = []

    def summarize_delta(turns, focus_topic=None):
        summary_calls.append(copy.deepcopy(turns))
        summary_started.set()
        assert release_summary.wait(2)
        return "single-flight candidate plus delta"

    monkeypatch.setattr(compressor, "_generate_summary", summarize_delta)
    start = threading.Barrier(3)
    results = []
    errors = []

    def run_hard_gate():
        try:
            start.wait(2)
            results.append(compressor.compress(appended, current_tokens=200))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_hard_gate) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(2)
    assert summary_started.wait(1)
    deadline = time.monotonic() + 1
    waiter_count = 0
    while time.monotonic() < deadline:
        with compressor._compression_lock:
            active = compressor._compression_active
            waiter_count = int(active.get("waiters") or 0) if active else 0
        if waiter_count == 1:
            break
        time.sleep(0.005)
    assert waiter_count == 1
    release_summary.set()
    for thread in threads:
        thread.join(2)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(summary_calls) == 1
    assert len(results) == 2
    assert results[0] == results[1]
    assert appended[-2:][0]["content"] == "concurrent append user"


def test_model_or_session_namespace_change_rejects_candidate():
    compressor = _compressor()
    turns = _messages(4)
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": compressor._background_source_hash(turns),
        "count": len(turns),
        "focus_topic": "topic",
        "summary": "prepared summary",
    }

    compressor._session_id = "session-b"
    assert compressor._consume_background_candidate(turns, "topic") is None


def test_persistence_marker_does_not_change_semantic_fingerprint():
    messages = _messages(4)
    before = ContextCompressor._background_source_hash(messages)
    for message in messages:
        message["_db_persisted"] = True
    assert ContextCompressor._background_source_hash(messages) == before


def test_nested_transport_metadata_does_not_change_semantic_fingerprint():
    messages = [
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"},
                }
            ],
        }
    ]
    before = ContextCompressor._background_source_hash(messages)
    messages[0]["tool_calls"][0]["_transport_trace"] = "ephemeral"

    assert ContextCompressor._background_source_hash(messages) == before


def test_destructive_prefix_edit_rejects_candidate_and_falls_back_without_loss():
    compressor = _compressor()
    messages = _messages()
    window = compressor._background_window(messages)
    assert window is not None
    compressor._background_candidate = {
        "namespace": compressor._background_namespace(),
        "source_hash": window["source_hash"],
        "count": window["count"],
        "focus_topic": window["focus_topic"],
        "requested_focus_topic": None,
        "summary": "stale prepared summary",
    }
    messages[2]["content"] = "destructive edit before watermark"
    captured = []

    def foreground_summary(turns_to_summarize, focus_topic=None):
        captured.extend(turns_to_summarize)
        contents = " | ".join(
            str(turn.get("content") or "") for turn in turns_to_summarize
        )
        return f"fresh foreground summary: {contents}"

    compressor._generate_summary = foreground_summary
    result = compressor.compress(messages, current_tokens=200)

    assert any(
        message.get("content") == "destructive edit before watermark"
        for message in captured
    )
    assert messages[2]["content"] == "destructive edit before watermark"
    assert result[0] == messages[0]
    assert result[-2:] == messages[-2:]
    assert any(
        "destructive edit before watermark" in str(item.get("content"))
        for item in result
    )


def test_background_path_preserves_tool_call_result_group_at_tail_boundary(monkeypatch):
    compressor = _compressor()
    compressor.protect_last_n = 1
    messages = _messages(8) + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-tail",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\":\"x\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-tail", "content": "tool result"},
    ]

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            assert not any(turn.get("tool_call_id") == "call-tail" for turn in turns)
            return "prepared tool-safe summary"

    monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())
    assert compressor.maybe_prepare_background(messages, current_tokens=80)
    assert compressor.wait_for_background_preparation(1)
    monkeypatch.setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("tool-safe candidate started a second call")
        ),
    )

    result = compressor.compress(messages, current_tokens=200)

    assert result[-2:] == messages[-2:]
    assert result[-2]["tool_calls"][0]["id"] == "call-tail"
    assert result[-1]["tool_call_id"] == "call-tail"


def test_hard_gate_joins_matching_inflight_preparation_without_duplicate_call():
    compressor = _compressor()
    messages = _messages()
    worker_started = threading.Event()
    release_worker = threading.Event()
    calls = []

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            calls.append("background")
            worker_started.set()
            release_worker.wait(2)
            return "prepared summary"

    compressor._clone_for_background = lambda: Worker()

    def forbidden_foreground_summary(*args, **kwargs):
        calls.append("foreground")
        raise AssertionError("hard gate started a duplicate summarizer call")

    compressor._generate_summary = forbidden_foreground_summary
    assert compressor.maybe_prepare_background(messages, current_tokens=80)
    assert worker_started.wait(1)

    errors = []
    results = []

    def run_hard_gate():
        try:
            results.append(compressor.compress(messages, current_tokens=200))
        except Exception as exc:
            errors.append(exc)

    hard_gate = threading.Thread(target=run_hard_gate)
    hard_gate.start()
    time.sleep(0.05)
    assert hard_gate.is_alive()
    release_worker.set()
    hard_gate.join(2)

    assert not hard_gate.is_alive()
    assert errors == []
    assert results
    assert calls == ["background"]


def test_background_preparation_respects_failure_cooldown():
    compressor = _compressor()
    compressor._summary_failure_cooldown_until = time.monotonic() + 60
    setattr(
        compressor,
        "_clone_for_background",
        lambda: (_ for _ in ()).throw(AssertionError("worker must not start during cooldown")),
    )

    assert not compressor.maybe_prepare_background(_messages(), current_tokens=80)


def test_background_worker_failure_propagates_cooldown_to_parent():
    compressor = _compressor()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            return None

        def get_active_compression_failure_cooldown(self):
            return {
                "remaining_seconds": 60.0,
                "error": "auxiliary summarizer unavailable",
            }

    setattr(compressor, "_clone_for_background", lambda: Worker())

    assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert not compressor.wait_for_background_preparation(timeout=2)
    cooldown = compressor.get_active_compression_failure_cooldown()
    assert cooldown is not None
    assert cooldown["remaining_seconds"] > 50
    assert cooldown["error"] == "auxiliary summarizer unavailable"
    assert not compressor.maybe_prepare_background(_messages(), current_tokens=80)


def test_background_window_preserves_live_previous_summary_lineage():
    compressor = _compressor()
    compressor._previous_summary = "live lineage"
    setattr(
        compressor,
        "_find_latest_context_summary",
        lambda *args, **kwargs: (1, "persisted lineage"),
    )

    window = compressor._background_window(_messages())

    assert window is not None
    assert window["previous_summary"] == "live lineage"


def test_session_model_and_profile_boundaries_prevent_inflight_publish():
    for boundary in ("reset", "end", "model", "profile"):
        compressor = _compressor()
        release = threading.Event()
        started = threading.Event()
        finished = threading.Event()

        class Worker:
            _previous_summary = None

            def _generate_summary(self, turns, focus_topic=None):
                started.set()
                release.wait(2)
                finished.set()
                return "stale summary"

        setattr(compressor, "_clone_for_background", lambda: Worker())
        assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
        assert started.wait(1)
        if boundary == "reset":
            compressor.on_session_reset()
        elif boundary == "end":
            compressor.on_session_end("session-a", _messages())
        elif boundary == "model":
            compressor.update_model("other-model", context_length=240)
        else:
            compressor.bind_profile_identity("other-profile")
        release.set()
        assert finished.wait(1)
        assert compressor._background_candidate is None


def test_model_and_profile_changes_invalidate_already_published_candidate(monkeypatch):
    for boundary in ("model", "profile"):
        compressor = _compressor()

        class Worker:
            _previous_summary = None

            def _generate_summary(self, turns, focus_topic=None):
                return "published candidate"

        monkeypatch.setattr(compressor, "_clone_for_background", lambda: Worker())
        assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
        assert compressor.wait_for_background_preparation(1)
        assert compressor._background_candidate is not None

        if boundary == "model":
            compressor.update_model("post-publish-model", context_length=240)
        else:
            compressor.bind_profile_identity("post-publish-profile")

        assert compressor._background_candidate is None


def test_matching_concurrent_preparers_are_single_flight():
    compressor = _compressor()
    release = threading.Event()
    started = threading.Event()
    calls = []

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            calls.append(len(turns))
            started.set()
            release.wait(2)
            return "one summary"

    setattr(compressor, "_clone_for_background", lambda: Worker())
    barrier = threading.Barrier(3)
    results = []

    def schedule():
        barrier.wait()
        results.append(compressor.maybe_prepare_background(_messages(), current_tokens=80))

    threads = [threading.Thread(target=schedule) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    assert started.wait(1)
    for thread in threads:
        thread.join(1)
    assert sorted(results) == [False, True]
    assert len(calls) == 1
    release.set()
    assert compressor.wait_for_background_preparation(1)


def test_background_capacity_exhaustion_skips_model_call(monkeypatch):
    compressor = _compressor()

    class ExhaustedSlots:
        def acquire(self, blocking=False):
            return False

        def release(self):
            raise AssertionError("unacquired slot must not be released")

    monkeypatch.setattr(
        "agent.context_compressor._BACKGROUND_PREPARATION_SLOTS",
        ExhaustedSlots(),
    )
    setattr(
        compressor,
        "_clone_for_background",
        lambda: (_ for _ in ()).throw(
            AssertionError("capacity exhaustion must not clone a worker")
        ),
    )

    assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert compressor.wait_for_background_preparation(1) is False
    assert compressor._background_candidate is None


def test_hard_gate_uses_remaining_worker_budget_not_full_timeout(monkeypatch):
    compressor = _compressor()
    window = compressor._background_window(_messages())
    assert window is not None
    wait_calls = []

    class PendingDone:
        def wait(self, timeout):
            wait_calls.append(timeout)
            return False

        def is_set(self):
            return False

    done = PendingDone()
    window["generation"] = 1
    window["started_monotonic"] = 1000.0
    compressor._background_generation = 1
    compressor._background_active = window
    monkeypatch.setattr(compressor, "_background_done", done)
    monkeypatch.setattr(time, "monotonic", lambda: 1105.9)

    assert compressor._join_compatible_background_preparation(
        window["turns"], timeout=120.0
    ) is False
    assert wait_calls == [pytest.approx(14.1)]
    assert window["timed_out"] is True
    assert compressor._background_generation == 2

    # The same expired worker remains the active single flight until its thread
    # exits, but a second hard gate must not pay another wait budget.
    assert compressor._join_compatible_background_preparation(
        window["turns"], timeout=120.0
    ) is False
    assert wait_calls == [pytest.approx(14.1)]

    # A different manual-focus request is not the same timed-out operation.
    assert compressor._join_compatible_background_preparation(
        window["turns"], requested_focus_topic="different", timeout=120.0
    ) is None
    assert wait_calls == [pytest.approx(14.1)]


@pytest.mark.parametrize(
    ("effective_timeout", "explicit_timeout"),
    [
        (float("inf"), None),
        (float("nan"), None),
        (300.0, float("inf")),
        (300.0, float("nan")),
    ],
)
def test_non_finite_join_budget_fails_closed_without_waiting(
    monkeypatch,
    effective_timeout,
    explicit_timeout,
):
    compressor = _compressor()
    window = compressor._background_window(_messages())
    assert window is not None

    class MustNotWait:
        def wait(self, timeout):
            raise AssertionError(f"unsafe non-finite join wait: {timeout!r}")

        def is_set(self):
            return False

    window["generation"] = 1
    window["started_monotonic"] = 1000.0
    compressor._background_generation = 1
    compressor._background_active = window
    monkeypatch.setattr(compressor, "_background_done", MustNotWait())
    monkeypatch.setattr(time, "monotonic", lambda: 1010.0)
    monkeypatch.setattr(
        "agent.context_compressor._effective_aux_timeout",
        lambda task, timeout: effective_timeout,
    )

    assert compressor._join_compatible_background_preparation(
        window["turns"], timeout=explicit_timeout
    ) is False
    assert window["timed_out"] is True
    assert compressor._background_generation == 2


def test_public_hard_gate_joins_aged_worker_within_compression_deadline(monkeypatch):
    """The public hard gate must not abandon a worker before its own deadline."""
    compressor = _compressor()
    messages = _messages()
    window = compressor._background_window(messages)
    assert window is not None
    timeout_requests = []
    wait_calls = []

    monkeypatch.setattr(
        "agent.context_compressor._effective_aux_timeout",
        lambda task, timeout: timeout_requests.append((task, timeout)) or 420.0,
    )

    class CompletesWithinProviderBudget:
        def wait(self, timeout):
            wait_calls.append(timeout)
            compressor._background_candidate = {
                "namespace": compressor._background_namespace(),
                "source_hash": window["source_hash"],
                "count": window["count"],
                "requested_focus_topic": None,
                "summary": "aged worker summary",
            }
            return True

        def is_set(self):
            return False

    window["generation"] = 1
    window["started_monotonic"] = 1000.0
    compressor._background_generation = 1
    compressor._background_active = window
    monkeypatch.setattr(
        compressor,
        "_background_done",
        CompletesWithinProviderBudget(),
    )
    monkeypatch.setattr(time, "monotonic", lambda: 1145.0)
    monkeypatch.setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("public hard gate started a second summarizer call")
        ),
    )

    result = compressor.compress(messages, current_tokens=200)

    assert timeout_requests == [("compression", None)]
    assert wait_calls == [pytest.approx(420.0 + 5.0 - 145.0)]
    assert result is not messages
    assert any("aged worker summary" in str(item.get("content")) for item in result)
    assert window.get("timed_out") is not True


def test_timed_out_generation_ignores_late_result():
    compressor = _compressor()
    messages = _messages()
    release = threading.Event()
    started = threading.Event()

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            started.set()
            release.wait(2)
            return "late timed-out summary"

    setattr(compressor, "_clone_for_background", lambda: Worker())
    assert compressor.maybe_prepare_background(messages, current_tokens=80)
    assert started.wait(1)
    window = compressor._background_window(messages)
    assert window is not None

    assert compressor._join_compatible_background_preparation(
        window["turns"], timeout=0.0
    ) is False
    assert compressor._join_compatible_background_preparation(
        window["turns"], timeout=120.0
    ) is False

    release.set()
    assert compressor.wait_for_background_preparation(1) is False
    assert compressor._background_candidate is None


def test_hard_gate_timeout_preserves_raw_transcript():
    compressor = _compressor()
    messages = _messages()
    raw_snapshot = copy.deepcopy(messages)
    setattr(
        compressor,
        "_join_compatible_background_preparation",
        lambda *args, **kwargs: False,
    )
    setattr(
        compressor,
        "_generate_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("timeout must not start a duplicate foreground call")
        ),
    )

    result = compressor.compress(messages, current_tokens=200)

    assert result is messages
    assert result == raw_snapshot
    assert compressor._last_compress_aborted is True


def test_cancelled_worker_cannot_publish_candidate_or_overlap_replacement():
    compressor = _compressor()
    release = threading.Event()
    started = threading.Event()
    calls = []

    class Worker:
        _previous_summary = None

        def _generate_summary(self, turns, focus_topic=None):
            calls.append(len(turns))
            started.set()
            release.wait(2)
            return "late summary"

    setattr(compressor, "_clone_for_background", lambda: Worker())
    assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert started.wait(1)
    compressor.cancel_background_preparation()
    assert not compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert len(calls) == 1

    release.set()
    time.sleep(0.05)
    assert compressor._background_candidate is None
    assert compressor._background_active is None

    assert compressor.maybe_prepare_background(_messages(), current_tokens=80)
    assert compressor.wait_for_background_preparation(1)
    assert len(calls) == 2
