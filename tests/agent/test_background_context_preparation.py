import copy
import threading
import time
from typing import Any

import pytest

from agent.context_compressor import ContextCompressor


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

    def foreground_summary(turns, focus_topic=None):
        captured.extend(turns)
        return "fresh foreground summary"

    compressor._generate_summary = foreground_summary
    compressor.compress(messages, current_tokens=200)

    assert any(
        message.get("content") == "destructive edit before watermark"
        for message in captured
    )
    assert messages[2]["content"] == "destructive edit before watermark"


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
