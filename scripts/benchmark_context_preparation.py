#!/usr/bin/env python3
"""Synthetic benchmark for background context preparation.

This benchmark uses a deterministic sleeping summarizer. It measures the
critical-path hard-gate latency only, plus preparation latency separately. It
is not a provider/network benchmark and must not be reported as production
latency.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

from agent.context_compressor import ContextCompressor

RUNS = 7
DELAY_PER_MESSAGE_SECONDS = 0.006


def make_messages(count: int = 18) -> list[dict[str, Any]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index} " + ("x" * 180),
        }
        for index in range(count)
    ]


def make_compressor(session_id: str) -> ContextCompressor:
    compressor = ContextCompressor(
        model="benchmark-model",
        threshold_percent=0.5,
        protect_first_n=1,
        protect_last_n=2,
        summary_target_ratio=0.2,
        quiet_mode=True,
        config_context_length=100,
        max_tokens=10,
    )
    compressor.context_length = 100
    compressor.threshold_tokens = 50
    compressor.tail_token_budget = 20
    compressor.max_summary_tokens = 20
    compressor.bind_session_state(session_id=session_id)
    return compressor


def sleeping_summary(
    turns_to_summarize: list[dict[str, Any]],
    focus_topic: str | None = None,
) -> str:
    time.sleep(DELAY_PER_MESSAGE_SECONDS * len(turns_to_summarize))
    return f"synthetic summary for {len(turns_to_summarize)} messages"


class SleepingWorker:
    _previous_summary = None

    def _generate_summary(
        self,
        turns: list[dict[str, Any]],
        focus_topic: str | None = None,
    ) -> str:
        return sleeping_summary(turns, focus_topic)


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def benchmark_baseline() -> list[float]:
    timings = []
    for run in range(RUNS):
        compressor = make_compressor(f"baseline-{run}")
        setattr(compressor, "_generate_summary", sleeping_summary)
        started = time.perf_counter()
        compressor.compress(make_messages(), current_tokens=100)
        timings.append(time.perf_counter() - started)
    return timings


def benchmark_prepared() -> tuple[list[float], list[float]]:
    gate_timings = []
    preparation_timings = []
    for run in range(RUNS):
        compressor = make_compressor(f"prepared-{run}")
        setattr(compressor, "_clone_for_background", lambda: SleepingWorker())
        messages = make_messages()
        prep_started = time.perf_counter()
        if not compressor.maybe_prepare_background(messages, current_tokens=80):
            raise RuntimeError("background preparation did not start")
        if not compressor.wait_for_background_preparation(timeout=5):
            raise RuntimeError("background preparation did not publish")
        preparation_timings.append(time.perf_counter() - prep_started)
        setattr(
            compressor,
            "_generate_summary",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("exact prepared hit started a second summarizer call")
            ),
        )
        gate_started = time.perf_counter()
        compressor.compress(messages, current_tokens=100)
        gate_timings.append(time.perf_counter() - gate_started)
    return gate_timings, preparation_timings


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(values) * 1000, 3),
        "p95_ms": round(percentile(values, 0.95) * 1000, 3),
        "min_ms": round(min(values) * 1000, 3),
        "max_ms": round(max(values) * 1000, 3),
    }


def main() -> None:
    baseline = benchmark_baseline()
    prepared_gate, preparation = benchmark_prepared()
    baseline_median = statistics.median(baseline)
    prepared_median = statistics.median(prepared_gate)
    prepared_total_if_not_hidden = [
        prep + gate for prep, gate in zip(preparation, prepared_gate, strict=True)
    ]
    prepared_total_median = statistics.median(prepared_total_if_not_hidden)
    result = {
        "kind": "synthetic_deterministic_sleep",
        "runs": RUNS,
        "delay_per_summarized_message_ms": DELAY_PER_MESSAGE_SECONDS * 1000,
        "baseline_hard_gate": summarize(baseline),
        "prepared_hard_gate": summarize(prepared_gate),
        "background_preparation_off_critical_path": summarize(preparation),
        "prepared_total_if_preparation_is_not_hidden": summarize(
            prepared_total_if_not_hidden
        ),
        "synthetic_hard_gate_speedup_excluding_preparation": round(
            baseline_median / prepared_median,
            2,
        ),
        "synthetic_total_speedup_if_preparation_is_not_hidden": round(
            baseline_median / prepared_total_median,
            2,
        ),
        "preparation_excluded_from_hard_gate_speedup": True,
        "disclaimer": (
            "Synthetic local timing only. The hard-gate speedup excludes the "
            "background preparation cost and applies only when preparation finishes "
            "before the gate. No provider, network, Gateway restart, or live "
            "Mattermost request was used."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
