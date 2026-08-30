# Agent of Empires ACP integration

- Status: live implementation; runtime promotion requires an ACP worker respawn.
- Verified against: Hermes ACP adapter and Agent of Empires structured sessions,
  2026-08-30.

## Delegation lifecycle contract

Gateway and CLI runtimes drain Hermes' async-delegation completion queue and
turn each completed child into a fresh parent turn. Agent of Empires connects
through `acp_adapter`, which does not own that queue watcher. Therefore the ACP
adapter scopes top-level `delegate_task` calls to synchronous execution for the
duration of `run_conversation` and restores the agent flag afterward.

This prevents a required review from producing `prompt_complete` while the
session remains incomplete (for example, an `Idle` plan at 8/9 waiting for a
background result). Gateway and CLI retain their native asynchronous behavior.

## Compression lifecycle contract

Hermes can spend minutes summarizing a high-context conversation before the
next provider turn. The ACP adapter maps the existing compression callbacks to
the lifecycle markers Agent of Empires understands:

- `Compacting...` when compression begins;
- `Compacting completed.` when `session:compress` confirms success;
- `Compacting failed.` if the turn exits without a completion event.

The adapter chains and restores any callback already installed on the agent.
Other Hermes status messages remain out of the ACP transcript.

## Local verification

| Check | Result |
|---|---|
| ACP scopes the flag during a turn and restores prior agent state | pass |
| Model-facing dispatch respects the ACP synchronous override | pass |
| Async delegation regression suite | 21 passed |
| Compression lifecycle start, completion, failure, and callback restoration | pass |
| ACP server regression suite | 88 passed |
| Ruff on changed runtime and tests | pass |
