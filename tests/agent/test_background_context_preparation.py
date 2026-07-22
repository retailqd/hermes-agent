import threading
import time
from typing import Any

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


def test_session_boundaries_prevent_inflight_worker_from_publishing():
    for boundary in ("reset", "end"):
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
        else:
            compressor.on_session_end("session-a", _messages())
        release.set()
        assert finished.wait(1)
        assert compressor._background_candidate is None


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
