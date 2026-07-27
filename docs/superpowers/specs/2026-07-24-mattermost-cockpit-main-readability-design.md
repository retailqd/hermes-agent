# Mattermost cockpit owner relay readability design

Date: 2026-07-24
Status: Approved by the owner's standing scope and the explicit owner-facing contract in task `cockpit-main-readability-20260724`

## Problem

The cockpit watcher currently forwards the textual stdout of the legacy polling helper directly into the owner-facing `main` thread. That stdout is an operator diagnostic format, not a presentation contract. It concatenates post counts, timestamps, usernames, Mattermost IDs, internal cockpit markers, status banners, raw HTTP details, gate tokens, commands, and the useful business answer into one post.

The root cause is the missing presentation boundary in `CockpitService.watch_once()`. The service truncates the helper output and prepends another visible marker, but does not classify, sanitize, or render owner-facing information.

## Goal

Every cockpit relay posted in `main` must be understandable at a glance and contain only:

1. the current state in plain PT-BR;
2. what completed, or why execution stopped;
3. one concrete next action or decision, only when required;
4. one permalink to the technical execution.

Owner-facing Markdown must use short headings, no oversized status heading, no operational English, no em dash, and no more than one decision CTA.

## Non-goals

- Do not redesign the technical `Execuções` thread.
- Do not hide technical evidence from `Execuções`.
- Do not alter owner decision binding, gate authorization, watcher leases, follow policy, close ordering, or company isolation.
- Do not modify the polling or owner-post skills.
- Do not alter Hermes config, auth, environment, active skills, plugins, cron jobs, systemd units, or the running Gateway during implementation.
- Do not activate the new runtime behavior without explicit owner approval in the source thread.

## Evidence

The source screenshot shows one bot relay with five raw posts. The visually dominant content is an English `Interrupting current task` banner. The useful business state is that the conversion was not applied because authentication was missing, and one authorization was needed. The screenshot also exposes timestamps, authors, Mattermost post IDs, cockpit markers, a raw `401 Unauthorized`, and an internal gate token.

Code evidence:

- `hermes_cli/mattermost_cockpit/helpers.py` correctly treats helper stdout as process output.
- `poll_main.py` emits an operator report with counts, timestamps, usernames, IDs, separators, and raw message bodies.
- `hermes_cli/mattermost_cockpit/service.py::watch_once()` currently copies that report into `main`.
- `create()`, `open_gate()`, and `close()` also place idempotency markers in visible message bodies.

## Options considered

### Option A: Regex-sanitize polling stdout

Parse the helper's formatted text, strip known lines, then relay the remainder.

Pros:

- smallest code change;
- preserves the current helper call.

Cons:

- remains coupled to a human diagnostic format;
- brittle when the helper changes punctuation, language, or separators;
- cannot reliably distinguish business text from technical detail;
- likely to leak new banner classes later.

Decision: rejected.

### Option B: Relay only lifecycle events

Publish only create, gate, and close messages. Never publish watcher updates.

Pros:

- strongest privacy and readability boundary;
- deterministic and easy to test.

Cons:

- loses meaningful state changes during long executions;
- a task can look stale between create and gate or close.

Decision: safe fallback, but incomplete as the primary design.

### Option C: Typed lifecycle rendering plus opt-in semantic watcher updates

Render create, gate, decision state, and close from typed service data. Keep the helper only for wake and baseline behavior. Read new Mattermost posts from the bound execution thread, and relay an intermediate update only when an execution post explicitly opts into the owner contract with a recognized semantic kind. Ambiguous or routine posts remain technical-only.

Pros:

- deterministic owner-facing boundary;
- no dependency on helper stdout formatting;
- meaningful phase changes remain possible;
- fail-closed behavior prevents accidental leaks;
- supports exact unit fixtures.

Cons:

- requires a small explicit semantic update contract for execution agents;
- legacy free-form technical posts no longer become owner updates.

Decision: selected.

## Architecture

### 1. Pure renderer

Add `hermes_cli/mattermost_cockpit/relay_renderer.py` with no network, filesystem, store, or process dependencies.

The module owns:

- PT-BR templates for `started`, `update`, `blocked`, `decision`, `succeeded`, `failed`, and `cancelled`;
- visible-text normalization;
- forbidden-internal-content checks;
- semantic execution-post classification;
- one-link and one-CTA enforcement;
- deterministic length bounding.

A small immutable result type carries:

- `body`: visible Mattermost Markdown;
- `kind`: lifecycle or semantic update kind;
- `requires_decision`: boolean.

The renderer never receives secrets, tokens, authorization headers, environment contents, or complete exception payloads.

### 2. Invisible idempotency metadata

Internal relay markers must not appear in new owner-facing message bodies.

Extend `MattermostClient.create_post()` with an optional `props` mapping and write flat metadata:

```json
{
  "cockpit_relay_marker": "<internal marker>",
  "cockpit_relay_schema": 1
}
```

`CockpitService._ensure_source_relay()` must:

1. find new relays by `props.cockpit_relay_marker`;
2. continue recognizing legacy markers in message bodies;
3. accept the exact legacy body on replay;
4. create new posts with visible Markdown only and metadata in `props`;
5. read the post back and verify channel, root, author, visible body, and marker metadata.

Execution-thread markers may remain visible because `Execuções` is the technical evidence surface. Owner decision relays into `Execuções` continue to preserve the exact owner text.

### 3. Lifecycle render paths

#### Create

Input: task title and execution permalink.

Output shape:

```markdown
**Em andamento**
<plain task title>

[Abrir detalhes técnicos](<permalink>)
```

#### Gate

Input: a semantic gate prompt and execution permalink.

Preferred input shape:

```markdown
**Bloqueado**
<plain reason execution stopped>

**Preciso de você**
<one concrete decision or action>
```

The renderer accepts this structure, removes internal markers, bounds each section, and appends one technical permalink. If the prompt does not contain a valid single decision section, gate creation fails closed before posting an ambiguous owner request.

Output shape:

```markdown
**Bloqueado**
<plain reason>

**Preciso de você**
<one action>

[Abrir detalhes técnicos](<permalink>)
```

#### Owner decision

The owner reply already exists in the source `main` thread. It must not be echoed back into `main`. `resume_owner_message()` continues to copy the exact validated owner text once into the bound execution root and keeps its existing durable gate deduplication.

#### Close

Succeeded:

```markdown
**Concluído**
<terminal business result>

**Validado**
<short validation evidence, when supplied>

[Abrir detalhes técnicos](<permalink>)
```

Failed or cancelled:

```markdown
**Interrompido**
<plain reason or terminal result>

**Próximo passo**
<one action only when required>

[Abrir detalhes técnicos](<permalink>)
```

A failed close with no owner action omits the next-step section.

### 4. Watcher update path

`watch_once()` keeps the existing watcher, follow, cursor, and polling lifecycle but never relays `poll.stdout`.

After the helper returns, the service reads the exact bound execution thread and selects posts whose `create_at` is newer than `execution_cursor_ms`.

It ignores:

- the execution root kickoff;
- owner decision relay markers;
- cockpit evidence and runtime binding markers;
- tool logs;
- `Interrupting`, compression, working, watcher, wake, heartbeat, and similar routine banners;
- messages with raw timestamps, author IDs, post IDs, HTTP diagnostics, gate tokens, shell commands, or authorization material;
- any unstructured or ambiguous post.

An intermediate relay is eligible only when a technical post starts with one recognized execution-only marker:

- `[cockpit-owner-state]`
- `[cockpit-owner-blocked]`

The marker is removed before rendering. The remaining body must match the same semantic sections used by lifecycle events. At most the latest eligible semantic update from one watcher cycle is posted to `main`. All other posts remain in `Execuções`.

The execution cursor advances across ignored posts as it does today, so routine noise is not reconsidered forever.

### 5. Visible text rules

The renderer enforces these invariants:

- maximum complete body length: 1,200 Unicode characters, including link;
- at most three short bold headings;
- exactly one technical permalink;
- at most one owner decision or next-action section;
- PT-BR system headings only;
- no em dash;
- no cockpit markers in visible output;
- no Mattermost IDs, author IDs, raw timestamps, post counts, separators, or helper preambles;
- no routine status banners;
- no raw HTTP status or exception internals;
- no internal gate tokens or commands;
- preserve business identifiers such as `NF 000119`, client names, tenant names, and terminal result text when they do not match forbidden internal patterns.

Bounding must truncate at a paragraph or word boundary and preserve the final permalink. It must not split Markdown syntax.

## Error handling and fail-closed behavior

- Invalid create title: reject before posting.
- Invalid or ambiguous gate prompt: reject before opening or posting a gate.
- Invalid semantic watcher update: skip the relay, keep the technical post, and advance the cursor.
- More than one decision CTA: reject the owner-facing render.
- Forbidden internal material in a lifecycle input: reject before posting rather than partially leaking it.
- Missing permalink: reject the render.
- Missing or tampered relay metadata on readback: keep the task non-terminal or blocked under the existing retry rules.
- Legacy visible markers: accept only for exact pre-existing replay compatibility, never emit them in a new post.

No renderer error may create a replacement execution root, infer approval, duplicate an owner decision, or terminalize a task before existing cleanup gates pass.

## Testing strategy

Follow test-driven development. Every production behavior starts with a focused failing test.

### Pure renderer tests

Deterministic fixtures must prove:

- started output is compact PT-BR and includes one technical link;
- a five-post screenshot-equivalent fixture excludes post count, timestamp, usernames, IDs, cockpit markers, `Interrupting`, compression, working, raw HTTP status, and internal gate token;
- owner-authored business text remains unchanged in the source fixture and is not duplicated into a relay;
- a semantic blocker preserves the plain business reason;
- one decision survives and a second decision is rejected;
- terminal success result and validation survive;
- failed and cancelled terminal outputs are PT-BR;
- business identifiers such as `NF 000119` survive;
- em dash is normalized out;
- output is no more than 1,200 characters and retains a valid final link;
- ambiguous technical text produces no owner relay.

### Client and service tests

Deterministic fixtures must exercise:

- `create` visible rendering with marker only in post props;
- create replay against both new props metadata and a legacy visible marker;
- gate rendering, durable reservation, retry, and exact one-CTA output;
- owner decision relay remains exact and is not duplicated in `main`;
- watcher ignores helper stdout even when it contains five raw posts;
- watcher selects only the latest recognized semantic execution post;
- ignored routine posts still advance the cursor;
- close success, failure, and retry rendering;
- tampered metadata fails closed;
- existing follow/unfollow, watcher lease, cleanup order, and dedupe tests remain green.

### Validation commands

Targeted:

```bash
python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py \
  tests/hermes_cli/test_mattermost_cockpit_client.py \
  tests/hermes_cli/test_mattermost_cockpit_store.py \
  tests/hermes_cli/test_mattermost_cockpit_service.py \
  tests/hermes_cli/test_mattermost_cockpit_cli.py
```

Static checks:

```bash
uvx ruff check \
  hermes_cli/mattermost_cockpit/relay_renderer.py \
  hermes_cli/mattermost_cockpit/client.py \
  hermes_cli/mattermost_cockpit/service.py \
  tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py \
  tests/hermes_cli/test_mattermost_cockpit_client.py \
  tests/hermes_cli/test_mattermost_cockpit_service.py
```

Broader cockpit and gateway-adjacent tests run after targeted tests pass.

## Mattermost Markdown validation

Code and tests must first validate exact Markdown strings locally. If live validation is safe, post one clearly labeled renderer fixture only in the bound technical `Execuções` root, inspect the rendered post in Mattermost desktop and narrow viewport, then preserve its permalink as evidence. Do not post a validation fixture into `main`, do not change Gateway behavior, and do not expose internal metadata in the rendered body.

If a live fixture is not safe, use the existing source screenshot plus browser-rendered local Markdown evidence and state the limitation.

## Version control and integration

- Work in an isolated worktree and branch.
- Keep the unrelated follow/unfollow staged changes untouched in the original checkout.
- Commit the approved design separately.
- Implement with RED, GREEN, REFACTOR commits or a small reviewed commit series.
- Rebase or merge the completed follow/unfollow commit before final review because both changes touch `service.py` and its tests.
- Inspect repository workflow triggers before push. Do not use GitHub-hosted Actions.
- Push to the `retailqd` remote only after tests and independent review pass.

## Protected activation boundary

Implementation may change versioned source and tests under the approved scope. Activation remains separate.

Before activation, produce two artifacts without applying them:

1. an exact protected policy diff that teaches execution agents to emit only `[cockpit-owner-state]` or `[cockpit-owner-blocked]` semantic blocks for intermediate owner updates;
2. a Gateway activation and rollback plan bound to the reviewed commit.

The proposed protected diff may target active SOUL or equivalent runtime policy surfaces, but must not be applied without explicit owner approval in the source thread.

The Gateway must not be restarted or reloaded during implementation or local validation. After approval, activation must wait for zero active agents, back up affected runtime state, use the established durable maintenance path, validate real create, gate, decision, close, rendered Markdown, watcher cleanup, and rollback if any owner-facing invariant fails.

## Acceptance criteria

The implementation is ready for activation review only when:

1. raw IDs, markers, timestamps, authors, status banners, HTTP internals, gate tokens, and commands are excluded from new owner-facing messages;
2. business state, terminal result, blocker reason, and one decision survive;
3. every system heading is PT-BR;
4. every body is at most 1,200 characters;
5. create, gate, decision, watcher update, and close fixtures are deterministic and green;
6. legacy relay replay remains idempotent;
7. existing cockpit lifecycle, follow, lease, gate, and cleanup tests remain green;
8. actual Mattermost Markdown shape is inspected when safe;
9. code is independently reviewed, committed, and pushed safely;
10. protected activation diff and restart plan are exact and unapplied;
11. activation still requires explicit owner approval.
