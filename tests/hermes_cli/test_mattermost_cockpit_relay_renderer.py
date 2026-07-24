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
        "Authorization: Bearer ***",
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
