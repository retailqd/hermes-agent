# Shared Core Prepared Context Compaction · Mattermost Release Gate

Date: 2026-07-22

## Scope and ownership map

- **Mattermost transport owner:** `plugins/platforms/mattermost/adapter.py` parses the post and preserves the Mattermost root thread identity.
- **Gateway session owner:** `gateway/run.py` maps the Mattermost source to a stable session key and caches one `AIAgent` per session/configuration signature.
- **Shared context owner:** the cached `AIAgent` owns `agent.context_compressor.ContextCompressor` and the live transcript used by `agent.conversation_loop`.
- **Prepared candidate owner:** the per-session `ContextCompressor` owns the single in-flight worker and immutable prepared candidate.
- **Durable transcript owner:** session persistence and rotation remain in the existing AIAgent/Gateway path. The background worker receives a deep-copied snapshot and does not write, truncate, rotate, or persist the live transcript.
- **Studio remains separate:** `packages/server/src/services/hermes/run-chat/compression.ts` is not the owner of Gateway/Mattermost compaction and is outside this release.

## Local evidence required before release

Run from a clean worktree with an isolated `HERMES_HOME`:

```bash
HERMES_HOME="$(mktemp -d)" \
  .venv/bin/python -m pytest \
  tests/agent/test_background_context_preparation.py \
  tests/agent/test_context_compressor.py \
  tests/test_hermes_state_compression_checkpoints.py \
  -o 'addopts=' -q

HERMES_HOME="$(mktemp -d)" \
MATTERMOST_ALLOWED_CHANNELS='' \
MATTERMOST_REQUIRE_MENTION='' \
MATTERMOST_FREE_RESPONSE_CHANNELS='' \
  .venv/bin/python -m pytest \
  tests/gateway/test_agent_cache.py::TestAgentCacheLifecycle::test_mattermost_thread_cache_preserves_prepared_candidate \
  tests/gateway/test_mattermost.py \
  -o 'addopts=' -q

.venv/bin/python scripts/benchmark_context_preparation.py
```

The benchmark is synthetic and deterministic. It measures critical-path hard-gate latency with a sleeping fake summarizer, not provider or production latency. The local run on 2026-07-22 produced:

- synchronous baseline hard gate median: **84.482 ms**
- prepared candidate hard gate median: **0.411 ms**
- background preparation median, excluded from the hard-gate timing: **84.850 ms**
- synthetic hard-gate speedup excluding preparation: **205.79x**
- prepared total median when preparation is not hidden: **85.309 ms**
- synthetic total speedup when preparation is not hidden: **0.99x**

These seven-run numbers must not be presented as live Mattermost or provider timings. The hard-gate speedup applies only when preparation completed off the critical path before the gate.

## Residual local validation limit

The local Mattermost cache test proves that the same cached `AIAgent` retains its prepared candidate, but it seeds the cache directly. It does not drive the complete live adapter and `_run_agent_inner` lookup path. Therefore the candidate is not releasable from local tests alone. The organic Mattermost gate below must verify real thread-to-session routing and same-agent reuse before production acceptance.

## Release decision required

This document stops before release. A new owner gate is required for:

1. deploying the candidate commit to the Gateway host;
2. one scoped Gateway restart or equivalent process replacement;
3. production log observation and Mattermost thread validation;
4. rollback if any acceptance criterion fails.

No config, auth, systemd, plugin, skill, memory, secret, schema, or transcript mutation is part of this release.

## Organic Mattermost validation

After explicit release approval:

1. Record the deployed immutable commit and the rollback commit.
2. Select one dedicated Mattermost thread whose real context is below the normal hard compaction threshold but close enough to cross the preparation threshold through ordinary conversation. Do not bulk-inject fabricated transcript content.
3. Record the thread root ID, current session ID/lineage, model, provider, profile, and pre-release message count. Do not record credentials or message bodies containing secrets.
4. Continue normal thread interaction until logs show:
   - `Background context preparation started`
   - `Background context preparation ready`
5. Confirm that readiness alone did not rotate the session, change the durable message count, or replace the live transcript.
6. Continue ordinary appends until the normal hard gate is reached.
7. Confirm one of these valid promotion paths:
   - exact hit: `Background context candidate consumed` with `tail=0` and no second summary provider call;
   - append-safe hit: candidate consumed with `tail>0`, followed by one summary call for only the appended suffix;
   - stale candidate: candidate rejected and the ordinary synchronous full-window path runs without message loss.
8. Confirm the post-gate transcript preserves:
   - protected head;
   - recent tail;
   - assistant tool-call plus tool-result groups atomically;
   - IDs, paths, approvals, decisions, and pending TODOs represented in the summary;
   - expected parent/child session lineage or in-place semantics according to the existing deployment setting.
9. Send one normal follow-up message in the same Mattermost root thread and verify coherent hydration, no duplicate assistant response, no orphaned tool result, and no unexpected reset.
10. Compare hard-gate timing to a pre-release synchronous baseline from the same provider/model class. Report only observed values, including sample size and failures.

## Acceptance criteria

- No background transcript mutation, persistence, or rotation before the hard gate.
- Candidate namespace matches session lineage, profile, model routing fingerprint, and compaction policy fingerprint.
- Normal append after the watermark remains reusable only when the semantic prefix hash matches.
- Destructive prefix edits, reset/end, model change, profile change, and policy change invalidate reuse.
- One worker per compressor generation, with at most four background summary calls process-wide. Capacity exhaustion skips preparation and leaves the normal hard-gate path intact.
- A matching hard gate joins its worker and never starts a duplicate summary call.
- A join timeout aborts compaction and returns the original transcript unchanged.
- Mattermost keeps the same cached `AIAgent` for the same thread/config signature, so preparation survives between turns.
- No regression in existing Mattermost adapter, context compressor, checkpoint, or cache tests.

## Rollback triggers

Rollback immediately if any of these occur:

- session rotation or message-count change before the hard gate;
- missing head/tail/tool-call group after promotion;
- cross-thread, cross-profile, cross-model, or cross-lineage candidate reuse;
- duplicate summary provider calls for the same prefix;
- stale candidate accepted after a destructive prefix mutation;
- repeated Gateway errors, duplicate Mattermost replies, failed thread hydration, or unrecoverable hard-gate timeout.

Rollback is code-only to the recorded immutable previous commit. Investigate before any config or service topology change.
