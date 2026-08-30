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

## Local verification

| Check | Result |
|---|---|
| ACP scopes the flag during a turn and restores prior agent state | pass |
| Model-facing dispatch respects the ACP synchronous override | pass |
| Async delegation regression suite | 21 passed |
| ACP server regression suite | 87 passed |
| Ruff on changed runtime and tests | pass |

