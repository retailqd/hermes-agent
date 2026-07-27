# Mattermost Cockpit Human-Readable Main Relays Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace raw technical cockpit relays in Mattermost `main` with compact, deterministic PT-BR lifecycle messages and opt-in semantic updates while preserving durable routing, gates, idempotency, and technical evidence in `Execuções`.

**Architecture:** A pure renderer owns all visible owner-facing Markdown and rejects ambiguous or technical input. `CockpitService` renders typed create, gate, and close events, stores idempotency markers in Mattermost post props, and treats the helper poll output only as a wake/baseline signal. Watcher updates are fail-closed and may relay only explicitly marked semantic execution posts.

**Tech Stack:** Python 3.13, standard library `dataclasses`, `enum`, and `re`, Mattermost REST API v4, SQLite-backed cockpit store, pytest, ruff.

## Global Constraints

- Work only in `/home/pht2/.hermes/worktrees/mattermost-cockpit-main-readability` on branch `fix/mattermost-cockpit-main-readability`.
- Keep `/home/pht2/.hermes/hermes-agent` and its staged follow/unfollow changes untouched.
- `APPROVED_BY_OWNER: standing-scope` covers versioned code, tests, local validation, commits, safe integration, and push.
- Do not modify Hermes config, auth, environment, active skills, plugins, cron jobs, systemd units, or runtime control surfaces.
- Do not restart or reload Gateway.
- Do not use GitHub-hosted Actions. GitHub Actions budget remains US$0.
- New owner-facing bodies are at most 1,200 Unicode characters, include exactly one technical permalink, contain at most one decision CTA, use PT-BR system headings, and contain no em dash.
- New owner-facing bodies must not expose cockpit markers, Mattermost IDs, author IDs, raw timestamps, post counts, routine banners, raw HTTP internals, gate tokens, shell commands, authorization material, or helper diagnostics.
- Preserve business identifiers such as `NF 000119`, client names, terminal result, blocker reason, and one concrete decision.
- Unsupported watcher content stays in `Execuções` and produces no `main` relay.
- New source relay markers live in Mattermost post props. Exact legacy visible-marker replays remain accepted.
- Owner decisions remain in the source `main` thread and are copied exactly once to the bound execution root. They are never echoed back into `main`.
- Use test-driven development. Observe every focused test fail for the expected missing behavior before implementing it.
- Use a frontend/UX specialist for Task 1 and a Hermes/Mattermost backend specialist for Tasks 2 through 4. Each task receives spec compliance review and code quality review before acceptance.

---

### Task 1: Pure owner relay renderer and UX contract

**Files:**
- Create: `hermes_cli/mattermost_cockpit/relay_renderer.py`
- Create: `tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py`

**Interfaces:**
- Consumes: plain lifecycle data and one execution permalink.
- Produces:
  - `RelayKind(str, Enum)` with `STARTED`, `UPDATE`, `BLOCKED`, `SUCCEEDED`, `FAILED`, `CANCELLED`.
  - `RenderedRelay(kind: RelayKind, body: str, requires_decision: bool = False)`.
  - `render_started(title: str, permalink: str) -> RenderedRelay`.
  - `render_gate(prompt: str, permalink: str) -> RenderedRelay`.
  - `render_closed(*, outcome: str, summary: str, validation: str | None, permalink: str) -> RenderedRelay`.
  - `render_execution_update(message: str, permalink: str) -> RenderedRelay | None`.
  - `MAX_RELAY_CHARS = 1200`.

- [ ] **Step 1: Write failing tests for started, blocker, terminal, sanitizer, language, CTA, and length contracts**

Create `tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py` with focused fixtures:

```python
from __future__ import annotations

import pytest

from hermes_cli.mattermost_cockpit.relay_renderer import (
    MAX_RELAY_CHARS,
    RelayKind,
    render_closed,
    render_execution_update,
    render_gate,
    render_started,
)

PERMALINK = "https://mattermost.example.com/pht/pl/execroot123"
FORBIDDEN = (
    "## 5 new posts",
    "retailqd",
    "[cockpit-relay:",
    "[cockpit-decision:",
    "[cockpit-owner-blocked]",
    "2026-07-24T17:25:30",
    "post-id-123",
    "Interrupting current task",
    "compression started",
    "working...",
    "watcher heartbeat",
    "401 Unauthorized",
    "gate-auth-shared-client-default",
    "$ hermes-mattermost-cockpit",
    "—",
)


def assert_owner_contract(body: str) -> None:
    assert len(body) <= MAX_RELAY_CHARS
    assert body.count("[Abrir detalhes técnicos]") == 1
    assert body.endswith(f"[Abrir detalhes técnicos]({PERMALINK})")
    assert not any(value in body for value in FORBIDDEN)


def test_render_started_is_compact_pt_br() -> None:
    relay = render_started("Corrigir conversão da NF 000119", PERMALINK)
    assert relay.kind is RelayKind.STARTED
    assert relay.requires_decision is False
    assert relay.body == (
        "**Em andamento**\n"
        "Corrigir conversão da NF 000119\n\n"
        f"[Abrir detalhes técnicos]({PERMALINK})"
    )
    assert_owner_contract(relay.body)


def test_render_gate_preserves_business_blocker_and_one_decision() -> None:
    prompt = """[cockpit-gate:task:gate-auth-shared-client-default]
**Bloqueado**
A correção não foi aplicada porque o conversor de PDF exige uma credencial que este cliente ainda não possui.

**Preciso de você**
Autorizar o uso da credencial compartilhada no cliente `default` para concluir e validar a NF 000119.
"""
    relay = render_gate(prompt, PERMALINK)
    assert relay.kind is RelayKind.BLOCKED
    assert relay.requires_decision is True
    assert "A correção não foi aplicada" in relay.body
    assert "Autorizar o uso da credencial compartilhada" in relay.body
    assert "NF 000119" in relay.body
    assert relay.body.count("**Preciso de você**") == 1
    assert_owner_contract(relay.body)


def test_render_gate_rejects_ambiguous_second_decision() -> None:
    prompt = """**Bloqueado**
Falta autorização.

**Preciso de você**
Autorizar A.

**Preciso de você**
Autorizar B.
"""
    with pytest.raises(ValueError, match="exactly one decision"):
        render_gate(prompt, PERMALINK)


def test_render_closed_preserves_terminal_result_and_validation() -> None:
    relay = render_closed(
        outcome="SUCCEEDED",
        summary="Conversão aplicada na NF 000119.",
        validation="PDF gerado e conferido no pedido correto.",
        permalink=PERMALINK,
    )
    assert relay.kind is RelayKind.SUCCEEDED
    assert relay.body.startswith("**Concluído**\nConversão aplicada")
    assert "**Validado**\nPDF gerado e conferido" in relay.body
    assert_owner_contract(relay.body)


@pytest.mark.parametrize(
    ("outcome", "heading", "kind"),
    [
        ("FAILED", "**Interrompido**", RelayKind.FAILED),
        ("CANCELLED", "**Interrompido**", RelayKind.CANCELLED),
    ],
)
def test_render_closed_failure_variants_are_pt_br(outcome, heading, kind) -> None:
    relay = render_closed(
        outcome=outcome,
        summary="A execução terminou sem aplicar a alteração.",
        validation=None,
        permalink=PERMALINK,
    )
    assert relay.kind is kind
    assert relay.body.startswith(heading)
    assert "Failed" not in relay.body
    assert "Cancelled" not in relay.body
    assert_owner_contract(relay.body)


def test_semantic_update_filters_screenshot_equivalent_noise() -> None:
    raw = """## 5 new posts
2026-07-24T17:25:30 | retailqd | post-id-123
[cockpit-decision:task:gate-auth-shared-client-default]
Autorizo a correção da NF 000119.
---
Interrupting current task...
compression started
working...
watcher heartbeat
401 Unauthorized
$ hermes-mattermost-cockpit gate --gate-id gate-auth-shared-client-default
---
[cockpit-owner-blocked]
**Bloqueado**
A correção não foi aplicada porque faltou autenticação do conversor de PDF.

**Preciso de você**
Autorizar a credencial compartilhada para concluir a NF 000119.
"""
    relay = render_execution_update(raw, PERMALINK)
    assert relay is not None
    assert "faltou autenticação" in relay.body
    assert "Autorizar a credencial compartilhada" in relay.body
    assert_owner_contract(relay.body)


def test_unstructured_technical_update_fails_closed() -> None:
    assert render_execution_update("working... HTTP 503 from backend", PERMALINK) is None


@pytest.mark.parametrize(
    "unsafe",
    [
        "2026-07-24T17:25:30 estado alterado",
        "Authorization: Bearer secret-token",
        "Consultar https://internal.example.com/debug",
        "gate-auth-shared-client-default",
        "**Unexpected heading**\nTexto",
    ],
)
def test_semantic_update_rejects_extra_metadata_links_and_headings(unsafe: str) -> None:
    assert render_execution_update(f"[cockpit-owner-state]\n{unsafe}", PERMALINK) is None


def test_long_body_is_bounded_without_breaking_the_link() -> None:
    relay = render_closed(
        outcome="SUCCEEDED",
        summary="Resultado validado. " * 300,
        validation="Fluxo conferido. " * 300,
        permalink=PERMALINK,
    )
    assert len(relay.body) <= MAX_RELAY_CHARS
    assert relay.body.endswith(f"[Abrir detalhes técnicos]({PERMALINK})")
```

- [ ] **Step 2: Run the renderer tests and verify RED**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py
```

Expected: collection error with `ModuleNotFoundError: No module named 'hermes_cli.mattermost_cockpit.relay_renderer'`.

- [ ] **Step 3: Implement the renderer with a strict semantic boundary**

Create `relay_renderer.py`. Use these exact public names and the following implementation structure:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

MAX_RELAY_CHARS = 1200
_LINK_LABEL = "Abrir detalhes técnicos"
_MARKER_LINE = re.compile(r"(?m)^\[(?:cockpit-[^\]]+|cockpit:[^\]]+)\]\s*$")
_LONG_INTERNAL_ID = re.compile(r"\b[a-z0-9]{26}\b")
_RAW_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?")
_RAW_HTTP = re.compile(
    r"(?i)\b(?:HTTP(?:/\d(?:\.\d)?)?\s*)?[1-5]\d{2}\s+"
    r"(?:unauthorized|forbidden|not found|bad gateway|service unavailable|internal server error)\b"
)
_ROUTINE = re.compile(
    r"(?im)^(?:interrupting current task|working|watcher|wake|heartbeat|compression|"
    r"context compaction)(?:\b|\W)"
)
_HELPER_PREAMBLE = re.compile(r"(?im)^##\s+\d+\s+new posts?\b")
_SHELL_LINE = re.compile(r"(?m)^\s*(?:\$\s+|hermes-mattermost-cockpit\s+)")
_GATE_TOKEN = re.compile(r"(?i)\bgate[-_:][a-z0-9_.:-]+\b")
_AUTH_MATERIAL = re.compile(r"(?i)\b(?:authorization\s*:\s*bearer|bearer\s+\S+)")
_VISIBLE_URL = re.compile(r"(?i)(?:https?://|\[[^\]]+\]\([^\)]+\))")
_EXTRA_HEADING = re.compile(r"(?m)^\*\*[^*\n]+\*\*\s*$")
_SECTION = re.compile(r"(?m)^\*\*(Bloqueado|Preciso de você)\*\*\s*$")


class RelayKind(str, Enum):
    STARTED = "started"
    UPDATE = "update"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RenderedRelay:
    kind: RelayKind
    body: str
    requires_decision: bool = False


def _permalink(value: str) -> str:
    value = value.strip()
    if not value.startswith(("https://", "http://")) or any(ch in value for ch in "\r\n"):
        raise ValueError("execution permalink must be an absolute HTTP URL")
    return value


def _plain(value: str, *, field: str) -> str:
    value = value.replace("\r", "\n").replace("—", ", ")
    value = _MARKER_LINE.sub("", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    checks = (
        (_LONG_INTERNAL_ID, "an internal identifier"),
        (_RAW_TIMESTAMP, "a raw timestamp"),
        (_RAW_HTTP, "raw HTTP status"),
        (_ROUTINE, "routine runtime content"),
        (_HELPER_PREAMBLE, "a helper preamble"),
        (_SHELL_LINE, "a shell command"),
        (_GATE_TOKEN, "an internal gate token"),
        (_AUTH_MATERIAL, "authorization material"),
        (_VISIBLE_URL, "an extra link"),
        (_EXTRA_HEADING, "an unsupported heading"),
    )
    for pattern, reason in checks:
        if pattern.search(value):
            raise ValueError(f"{field} contains {reason}")
    return value


def _bounded(sections: list[str], permalink: str) -> str:
    link = f"[{_LINK_LABEL}]({_permalink(permalink)})"
    budget = MAX_RELAY_CHARS - len(link) - 2
    kept: list[str] = []
    for section in sections:
        separator = 2 if kept else 0
        available = budget - sum(len(item) for item in kept) - separator * len(kept)
        if available <= 0:
            break
        if len(section) > available:
            clipped = section[:available].rsplit(" ", 1)[0].rstrip(" .,:;") + "…"
            kept.append(clipped)
            break
        kept.append(section)
    body = "\n\n".join(kept + [link])
    if len(body) > MAX_RELAY_CHARS:
        raise AssertionError("owner relay length invariant failed")
    return body


def _sections(prompt: str) -> tuple[str, str]:
    prompt = _MARKER_LINE.sub("", prompt.replace("\r", "\n")).strip()
    matches = list(_SECTION.finditer(prompt))
    labels = [match.group(1) for match in matches]
    if labels.count("Bloqueado") != 1 or labels.count("Preciso de você") != 1:
        raise ValueError("gate prompt requires exactly one blocker and exactly one decision")
    if labels != ["Bloqueado", "Preciso de você"]:
        raise ValueError("gate prompt sections are out of order")
    blocker = prompt[matches[0].end() : matches[1].start()].strip()
    decision = prompt[matches[1].end() :].strip()
    return _plain(blocker, field="blocker"), _plain(decision, field="decision")


def render_started(title: str, permalink: str) -> RenderedRelay:
    body = _bounded([f"**Em andamento**\n{_plain(title, field='title')}"], permalink)
    return RenderedRelay(RelayKind.STARTED, body)


def render_gate(prompt: str, permalink: str) -> RenderedRelay:
    blocker, decision = _sections(prompt)
    body = _bounded(
        [f"**Bloqueado**\n{blocker}", f"**Preciso de você**\n{decision}"],
        permalink,
    )
    return RenderedRelay(RelayKind.BLOCKED, body, requires_decision=True)


def render_closed(
    *, outcome: str, summary: str, validation: str | None, permalink: str
) -> RenderedRelay:
    normalized = outcome.strip().upper()
    kind = {
        "SUCCEEDED": RelayKind.SUCCEEDED,
        "FAILED": RelayKind.FAILED,
        "CANCELLED": RelayKind.CANCELLED,
    }.get(normalized)
    if kind is None:
        raise ValueError("unsupported close outcome")
    heading = "Concluído" if kind is RelayKind.SUCCEEDED else "Interrompido"
    sections = [f"**{heading}**\n{_plain(summary, field='summary')}"]
    if validation:
        sections.append(f"**Validado**\n{_plain(validation, field='validation')}")
    return RenderedRelay(kind, _bounded(sections, permalink))


def render_execution_update(message: str, permalink: str) -> RenderedRelay | None:
    if "[cockpit-owner-blocked]" in message:
        semantic = message.split("[cockpit-owner-blocked]", 1)[1]
        return render_gate(semantic, permalink)
    if "[cockpit-owner-state]" in message:
        semantic = message.split("[cockpit-owner-state]", 1)[1]
        try:
            state = _plain(semantic, field="state")
        except ValueError:
            return None
        return RenderedRelay(
            RelayKind.UPDATE,
            _bounded([f"**Em andamento**\n{state}"], permalink),
        )
    return None
```

If a test reveals that the long-ID pattern rejects the configured fixture permalink, apply it only to visible section text, never to the permalink. Do not weaken the forbidden-content checks to make an unsafe fixture pass.

- [ ] **Step 4: Run renderer tests and verify GREEN**

Run the same focused command from Step 2.

Expected: all tests in `test_mattermost_cockpit_relay_renderer.py` pass.

- [ ] **Step 5: Run ruff on the new files**

Run:

```bash
uvx ruff check hermes_cli/mattermost_cockpit/relay_renderer.py tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py
```

Expected: `All checks passed!`.

- [ ] **Step 6: Commit the renderer**

```bash
git add hermes_cli/mattermost_cockpit/relay_renderer.py tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py
git commit -m "feat(cockpit): render compact owner relays"
```

---

### Task 2: Mattermost post props and invisible relay idempotency

**Files:**
- Modify: `hermes_cli/mattermost_cockpit/client.py:83-100`
- Modify: `hermes_cli/mattermost_cockpit/service.py:609-651`
- Modify: `tests/hermes_cli/test_mattermost_cockpit_client.py:155-176`
- Modify: `tests/hermes_cli/test_mattermost_cockpit_service.py:19-55,172-207,469-531`

**Interfaces:**
- Consumes: `message`, `marker`, and exact source thread binding.
- Produces:
  - `MattermostClient.create_post(channel_id, message, *, root_id=None, props=None)`.
  - `_source_relay_props(marker: str) -> dict[str, object]`.
  - `_post_relay_marker(post: Mapping[str, Any]) -> str | None`.
  - `_ensure_source_relay()` that emits visible body only and verifies props readback.

- [ ] **Step 1: Extend client and service fakes in tests, then write RED assertions for props**

Update the existing client test to call:

```python
payload = client.create_post(
    "channel-7",
    "hello",
    root_id="root-9",
    props={"cockpit_relay_marker": "relay-1", "cockpit_relay_schema": 1},
)
```

Assert the request body is:

```python
{
    "channel_id": "channel-7",
    "message": "hello",
    "root_id": "root-9",
    "props": {"cockpit_relay_marker": "relay-1", "cockpit_relay_schema": 1},
}
```

Extend `FakeMattermost.create_post()` in the service test to accept `props=None` and store a shallow copy under `post["props"]`.

Add service tests that assert:

```python
source_relay = next(post for post in bot.posts.values() if post["channel_id"] == MAIN)
assert "[cockpit-relay:" not in source_relay["message"]
assert source_relay["props"]["cockpit_relay_marker"].startswith("[cockpit-relay:")
assert source_relay["props"]["cockpit_relay_schema"] == 1
```

Add one legacy fixture whose visible body is `marker + "\n" + expected_message`; replay `create()` and assert no duplicate post is created.

Add one tamper fixture where props contain the correct marker but the body differs. Assert the service fails closed with `ValueError` and does not create another source relay.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_client.py::TestMattermostClient::test_create_post_uses_explicit_token_only \
  tests/hermes_cli/test_mattermost_cockpit_service.py -k 'create and (idempotent or marker or tampered)'
```

Expected: client fails because `create_post()` does not accept `props`; service assertions fail because the marker remains in the visible body.

- [ ] **Step 3: Add optional props to the REST client**

Change `MattermostClient.create_post()` to:

```python
def create_post(
    self,
    channel_id: str,
    message: str,
    *,
    root_id: str | None = None,
    props: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"channel_id": channel_id, "message": message}
    if root_id:
        body["root_id"] = root_id
    if props is not None:
        body["props"] = dict(props)
    return self._request_json_object("POST", "/api/v4/posts", body)
```

Import `Mapping` from `collections.abc` or `typing` according to the existing file style.

- [ ] **Step 4: Move new source markers to props with legacy read compatibility**

In `service.py`, add:

```python
_RELAY_SCHEMA = 1


def _source_relay_props(marker: str) -> dict[str, object]:
    return {"cockpit_relay_marker": marker, "cockpit_relay_schema": _RELAY_SCHEMA}


def _post_relay_marker(post: Mapping[str, Any]) -> str | None:
    props = post.get("props")
    if isinstance(props, Mapping):
        value = props.get("cockpit_relay_marker")
        if isinstance(value, str) and value:
            return value
    message = str(post.get("message", ""))
    first_line = message.splitlines()[0] if message else ""
    return first_line if first_line.startswith("[cockpit-") else None
```

Update `_ensure_source_relay()` so lookup checks `_post_relay_marker(post) == marker`. For a new post, call:

```python
created = self.bot_client.create_post(
    task.source_channel_id,
    message,
    root_id=task.source_root_id,
    props=_source_relay_props(marker),
)
```

Read the post back. Require exact visible `message`, exact channel/root/author, exact marker prop, and schema `1`.

For an existing legacy post, accept only exact `f"{marker}\n{message}"`. Never emit that form for a new post.

- [ ] **Step 5: Run the client and service tests and verify GREEN**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_client.py \
  tests/hermes_cli/test_mattermost_cockpit_service.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit invisible relay metadata**

```bash
git add hermes_cli/mattermost_cockpit/client.py hermes_cli/mattermost_cockpit/service.py \
  tests/hermes_cli/test_mattermost_cockpit_client.py tests/hermes_cli/test_mattermost_cockpit_service.py
git commit -m "fix(cockpit): hide relay markers in post props"
```

---

### Task 3: Render create, gate, and close lifecycle messages

**Files:**
- Modify: `hermes_cli/mattermost_cockpit/service.py:97-212,214-285,382-480`
- Modify: `tests/hermes_cli/test_mattermost_cockpit_service.py:172-207,376-468,553-604,658-768`

**Interfaces:**
- Consumes: Task 1 renderer and Task 2 post props.
- Produces: all create, gate, and close source relays through `RenderedRelay.body`, never through string concatenation with markers.

- [ ] **Step 1: Write RED service tests for exact create, gate, decision, and close body shapes**

Update or add tests with these assertions:

```python
start = source_posts(bot)[0]
assert start["message"] == (
    "**Em andamento**\nTask title\n\n"
    f"[Abrir detalhes técnicos]({service._permalink(EXEC_ROOT)})"
)
assert "cockpit" not in start["message"]
```

Gate fixture:

```python
prompt = """**Bloqueado**
A correção não foi aplicada porque faltou autenticação do conversor de PDF.

**Preciso de você**
Autorizar a credencial compartilhada para concluir a NF 000119.
"""
service.open_gate(task.task_id, gate_id="gate-auth", prompt=prompt)
gate_post = source_posts(bot)[-1]
assert gate_post["message"].startswith("**Bloqueado**")
assert gate_post["message"].count("**Preciso de você**") == 1
assert "gate-auth" not in gate_post["message"]
assert gate_post["message"].endswith(
    f"[Abrir detalhes técnicos]({service._permalink(EXEC_ROOT)})"
)
```

Add a test that calls `open_gate()` with `prompt="Pode aprovar?"` and asserts `ValueError` is raised before `store.get_active_gate()` returns a reservation and before a source post is created.

Keep the existing owner-decision assertion:

```python
assert relayed_decision["message"].endswith("\n" + decision["message"])
assert decision["message"] not in any_new_source_relay_body
```

Close success fixture:

```python
service.close(
    task.task_id,
    outcome=Lifecycle.SUCCEEDED,
    summary="Conversão aplicada na NF 000119.",
    evidence={"validation": "PDF gerado e conferido no pedido correto."},
)
final = source_posts(bot)[-1]
assert final["message"].startswith("**Concluído**\nConversão aplicada")
assert "**Validado**\nPDF gerado e conferido" in final["message"]
assert "[cockpit-final:" not in final["message"]
```

Add parameterized failed and cancelled close fixtures asserting the visible heading is `**Interrompido**`, no raw `last_error` appears, and the existing cleanup ordering remains unchanged.

- [ ] **Step 2: Run focused lifecycle tests and verify RED**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_service.py \
  -k 'create_is_idempotent or gate or decision or close'
```

Expected: failures show visible markers and legacy raw lifecycle text.

- [ ] **Step 3: Integrate renderer calls before durable side effects**

At the top of `service.py`, import:

```python
from .relay_renderer import render_closed, render_gate, render_started
```

In `create()`, after binding and permalink validation but before `_ensure_source_relay()`, compute:

```python
relay = render_started(task.title, permalink)
source_post = self._ensure_source_relay(
    task,
    marker=f"[cockpit-relay:{task.task_id}:started]",
    message=relay.body,
)
```

In `open_gate()`, render before reserving the gate:

```python
relay = render_gate(prompt, self._permalink(task.execution_root_id))
```

Store the normalized visible `relay.body` as `prompt_body` for new gates, and pass it to `_ensure_source_relay()` with the marker in props. Existing reserved legacy gates retain their stored exact prompt body and replay compatibility.

In `close()`, never pass `last_error` to the owner renderer. Extract validation only when it is a non-empty string:

```python
validation_value = evidence.get("validation")
validation = validation_value.strip() if isinstance(validation_value, str) else None
relay = render_closed(
    outcome=outcome.value,
    summary=summary,
    validation=validation,
    permalink=self._permalink(task.execution_root_id),
)
```

Use `relay.body` for `_ensure_source_relay()` and keep evidence details in the execution evidence post.

- [ ] **Step 4: Run lifecycle and full cockpit tests and verify GREEN**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py \
  tests/hermes_cli/test_mattermost_cockpit_client.py \
  tests/hermes_cli/test_mattermost_cockpit_store.py \
  tests/hermes_cli/test_mattermost_cockpit_service.py \
  tests/hermes_cli/test_mattermost_cockpit_cli.py
```

Expected: all cockpit tests pass.

- [ ] **Step 5: Commit lifecycle rendering**

```bash
git add hermes_cli/mattermost_cockpit/service.py tests/hermes_cli/test_mattermost_cockpit_service.py
git commit -m "fix(cockpit): render owner lifecycle in plain PT-BR"
```

---

### Task 4: Stop relaying helper stdout and allow only semantic watcher updates

**Files:**
- Modify: `hermes_cli/mattermost_cockpit/service.py:482-558`
- Modify: `tests/hermes_cli/test_mattermost_cockpit_service.py:769-812`

**Interfaces:**
- Consumes: `render_execution_update(message, permalink)` from Task 1 and the existing execution cursor.
- Produces: zero or one owner-facing semantic update per watcher cycle; ignored posts still advance the cursor.

- [ ] **Step 1: Write RED watcher fixtures matching the screenshot failure class**

Configure `bridge.poll_output` with the five-post diagnostic fixture from Task 1, including timestamps, usernames, post IDs, markers, `Interrupting current task`, raw HTTP status, gate token, command, and owner text. Do not add any eligible semantic post to `bot` thread data.

Assert:

```python
source_count = len(source_posts(bot))
assert service.watch_once(task.task_id) is True
assert len(source_posts(bot)) == source_count
assert store.get_task(task.task_id).execution_cursor_ms > old_cursor
```

Then add execution posts newer than the cursor:

```python
bot.posts["routine"] = execution_post(
    post_id="routine",
    create_at=old_cursor + 1,
    message="Interrupting current task... HTTP 503",
)
bot.posts["semantic-old"] = execution_post(
    post_id="semantic-old",
    create_at=old_cursor + 2,
    message="""[cockpit-owner-state]
A autenticação foi validada e a conversão começou.
""",
)
bot.posts["semantic-latest"] = execution_post(
    post_id="semantic-latest",
    create_at=old_cursor + 3,
    message="""[cockpit-owner-state]
A conversão terminou e o PDF está em validação.
""",
)
```

Assert exactly one new source relay exists, contains only the latest semantic state, contains one technical link, and excludes all forbidden fixture tokens.

Add a semantic blocker fixture with one valid decision and assert it renders `**Bloqueado**` and exactly one `**Preciso de você**`.

Add a malformed semantic fixture containing `401 Unauthorized` and assert it produces no relay but still advances the cursor.

- [ ] **Step 2: Run watcher tests and verify RED**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_service.py -k 'watch_once'
```

Expected: current implementation creates a source relay from `poll.stdout`, so the no-relay assertion fails and the raw diagnostic content is visible.

- [ ] **Step 3: Replace stdout relay with actual-thread semantic selection**

Import `render_execution_update` in `service.py`.

After `bridge.poll_main()` succeeds, ignore `poll.stdout`. Use the existing `get_thread(task.execution_root_id)` result and exact author/channel/root validation. Build `new_posts` with:

```python
new_posts = [
    post
    for post in ordered_thread_posts(thread)
    if int(post.get("create_at", 0) or 0) > task.execution_cursor_ms
]
```

Advance the cursor to the maximum validated `create_at` even when no post is eligible.

For each new post in chronological order, call:

```python
candidate = render_execution_update(
    str(post.get("message", "")),
    self._permalink(task.execution_root_id),
)
```

Keep only the latest non-`None` candidate. If it exists, publish with a deterministic marker derived from task ID and source post ID in props:

```python
marker = f"[cockpit-relay:{task.task_id}:update:{post['id']}]"
self._ensure_source_relay(task, marker=marker, message=candidate.body)
```

Do not include `poll.stdout`, post IDs, authors, timestamps, or marker text in `candidate.body`.

Preserve existing return behavior, watcher lease, terminal short-circuit, follow readback, cursor persistence, and error handling.

- [ ] **Step 4: Run watcher and full cockpit tests and verify GREEN**

Run the watcher command from Step 2, then the full cockpit command from Task 3 Step 4.

Expected: all tests pass and no helper diagnostic becomes owner-facing.

- [ ] **Step 5: Commit watcher semantic filtering**

```bash
git add hermes_cli/mattermost_cockpit/service.py tests/hermes_cli/test_mattermost_cockpit_service.py
git commit -m "fix(cockpit): relay only semantic watcher updates"
```

---

### Task 5: Protected activation artifacts, rendered Markdown evidence, review, and safe integration

**Files:**
- Create: `docs/superpowers/activation/2026-07-24-cockpit-main-readability-policy.patch`
- Create: `docs/superpowers/activation/2026-07-24-cockpit-main-readability-activation.md`
- Modify only if review finds a defect: files from Tasks 1 through 4.

**Interfaces:**
- Consumes: reviewed implementation commit and exact active policy file readbacks.
- Produces: an unapplied protected diff, an unapplied restart/rollback plan, validation evidence, reviewed commit series, and safe remote branch.

- [ ] **Step 1: Read active policy files and generate an exact unapplied patch artifact**

Read the current active SOUL/policy surfaces without modifying them. Build a unified patch file that adds this execution-post contract to the existing Mattermost cockpit section:

```markdown
### Semantic owner updates

Intermediate owner updates are opt-in and must be posted only in the bound `Execuções` root.

- Use `[cockpit-owner-state]` followed by one plain PT-BR state paragraph when a meaningful phase changes.
- Use `[cockpit-owner-blocked]`, then exactly one `**Bloqueado**` section and exactly one `**Preciso de você**` section when execution truly requires an owner decision.
- Keep routine work, watcher, wake, heartbeat, compression, tool, HTTP, IDs, tokens, and command details outside semantic owner blocks.
- Never place more than one owner decision in a semantic block.
```

The artifact must contain exact file paths and exact surrounding context from the readback. Do not run `patch`, `git apply`, `hermes config set`, `hermes doctor --fix`, restart commands, or any command that writes to protected surfaces.

- [ ] **Step 2: Write the unapplied activation and rollback plan**

The plan must state:

1. owner approval phrase required in the source thread;
2. exact versioned commit and candidate branch;
3. active-agent zero check before restart;
4. backup paths and checksums for each protected file and cockpit SQLite database;
5. exact protected patch preflight with `git apply --check` or `patch --dry-run` against copies, never the active file before approval;
6. durable Gateway restart path appropriate to the live deployment;
7. real create, semantic update, gate, owner decision, close, permalink, Markdown, watcher cleanup, and thread-follow validation;
8. rollback command sequence restoring policy files, versioned code, database backup when required, and prior service state;
9. fail-closed criteria for any visible marker, ID, English banner, raw HTTP status, duplicate CTA, body over 1,200 characters, duplicate root, or watcher leak.

Do not execute the plan.

- [ ] **Step 3: Run the complete targeted suite and static checks**

Run:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest -q -o 'addopts=' \
  tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py \
  tests/hermes_cli/test_mattermost_cockpit_client.py \
  tests/hermes_cli/test_mattermost_cockpit_store.py \
  tests/hermes_cli/test_mattermost_cockpit_service.py \
  tests/hermes_cli/test_mattermost_cockpit_cli.py

uvx ruff check \
  hermes_cli/mattermost_cockpit/relay_renderer.py \
  hermes_cli/mattermost_cockpit/client.py \
  hermes_cli/mattermost_cockpit/service.py \
  tests/hermes_cli/test_mattermost_cockpit_relay_renderer.py \
  tests/hermes_cli/test_mattermost_cockpit_client.py \
  tests/hermes_cli/test_mattermost_cockpit_service.py

git diff --check
git status --short
```

Expected: all tests pass, ruff reports `All checks passed!`, diff check is empty, and status lists only the two intended activation artifacts before their commit.

- [ ] **Step 4: Validate Mattermost Markdown without Gateway activation**

Preferred safe path:

1. render deterministic started, blocked, and succeeded fixtures locally;
2. post one clearly labeled fixture into this task's bound `Execuções` root only;
3. inspect desktop and narrow-width Mattermost rendering;
4. verify exactly one CTA, headings fit without visual dominance, no horizontal overflow, no raw metadata, and link opens the exact execution root;
5. capture the fixture permalink and screenshot as technical evidence;
6. do not post a validation fixture into `main`.

If a live fixture would disturb task routing, render the exact Markdown in a local Mattermost-compatible preview, compare with the source screenshot, and record the limitation in the activation plan.

- [ ] **Step 5: Request independent reviews**

Use `superpowers:requesting-code-review` and dispatch:

- one frontend/UX specialist to verify information hierarchy, PT-BR copy, bounded length, one CTA, and desktop/narrow-width rendering;
- one Hermes/Mattermost backend specialist to verify props idempotency, legacy replay, gate durability, cursor behavior, follow policy, cleanup order, and fail-closed handling.

Require exact file/line findings and PASS/FAIL verdicts. Fix every confirmed blocker with a new RED test, rerun focused and full checks, and commit the fix.

- [ ] **Step 6: Integrate the concurrent follow/unfollow task safely**

Inspect the original checkout and remote branch without modifying them:

```bash
git -C /home/pht2/.hermes/hermes-agent status --short --branch
git fetch retailqd
git log --oneline --decorate --max-count=10 retailqd/fix/mattermost-cockpit-main-only-threads
```

If the follow/unfollow changes have been committed and pushed, rebase this branch onto that remote branch:

```bash
git rebase retailqd/fix/mattermost-cockpit-main-only-threads
```

Resolve only overlapping cockpit code and tests. Preserve both invariants: source `main` thread is followed, execution root is unfollowed, and owner relay bodies remain human-readable. Rerun all Task 5 Step 3 checks after the rebase.

If the other task remains staged or unpushed, do not copy, reset, commit, or overwrite its changes. Keep this branch isolated and record integration as the only code blocker before push.

- [ ] **Step 7: Commit activation artifacts without applying them**

Because `docs/superpowers/*` is intentionally ignored, add only the exact artifacts with force:

```bash
git add -f \
  docs/superpowers/activation/2026-07-24-cockpit-main-readability-policy.patch \
  docs/superpowers/activation/2026-07-24-cockpit-main-readability-activation.md
git diff --cached --check
git commit -m "docs(cockpit): prepare guarded readability activation"
```

- [ ] **Step 8: Inspect workflow risk and push safely**

Read `.github/workflows` and repository branch protections. Confirm the push does not trigger GitHub-hosted runners. If any hosted workflow would run, do not push and report it as a migration blocker.

If safe:

```bash
git push -u retailqd fix/mattermost-cockpit-main-readability
```

Read back the remote branch exact SHA:

```bash
git ls-remote --heads retailqd fix/mattermost-cockpit-main-readability
```

Do not merge, deploy, restart, or apply the protected patch.

- [ ] **Step 9: Final evidence bundle**

Report:

- branch and exact remote SHA;
- commits created;
- targeted test count and command;
- ruff result;
- screenshot or rendered Markdown validation evidence;
- frontend/UX review verdict;
- Hermes/Mattermost backend review verdict;
- concurrent follow/unfollow integration state;
- exact protected artifacts and their SHA-256 checksums;
- explicit statement that no protected file was changed and Gateway was not restarted;
- exact owner approval still required for activation.
