from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_cli.mattermost_cockpit.presentation import (
    OwnerDecisionPrompt,
    OwnerMessageState,
    render_owner_blocked,
    render_owner_completed,
    render_owner_decision,
    render_owner_duplicate,
    render_owner_failed,
    render_owner_next_step,
    render_owner_progress,
    validate_owner_decision_prompt,
    validate_owner_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "mattermost_owner_message_cases.json"


def load_cases() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("# heading", "headings"),
        ("plain text\n[cockpit-secret]", "internal markers"),
        ("line\n" * 25, "line budget"),
        ("The fingerprint is visible", "technical terms"),
        ("The FINGERPRINT is visible", "technical terms"),
    ],
)
def test_validate_owner_markdown_rejects_headings_markers_budget_and_technical_terms(
    message: str,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        validate_owner_markdown(message, max_lines=24)


@pytest.mark.parametrize(
    ("plain_language", "risk", "reply_instruction"),
    [
        ("", "low", "aprovar"),
        ("aprovar", "", "aprovar"),
        ("aprovar", "low", ""),
        ("   ", "low", "aprovar"),
        ("aprovar", "   ", "aprovar"),
        ("aprovar", "low", "   "),
    ],
)
def test_validate_owner_decision_prompt_rejects_empty_gating_fields(
    plain_language: str,
    risk: str,
    reply_instruction: str,
) -> None:
    prompt = OwnerDecisionPrompt(
        decision="aprovar",
        plain_language=plain_language,
        risk=risk,
        reply_instruction=reply_instruction,
    )

    with pytest.raises(ValueError, match="non-empty"):
        validate_owner_decision_prompt(prompt)


@pytest.mark.parametrize(
    ("renderer", "kwargs", "kind", "state", "terminal"),
    [
        (
            render_owner_progress,
            {"now": "validando a causa", "next_milestone": "causa confirmada ou bloqueio"},
            "progress",
            OwnerMessageState.PROGRESS,
            False,
        ),
        (
            render_owner_next_step,
            {"now": "agendamento confirmado", "next_milestone": "executar no horário combinado"},
            "cron",
            OwnerMessageState.NEXT_STEP,
            False,
        ),
        (
            render_owner_decision,
            {
                "prompt": OwnerDecisionPrompt(
                    decision="aprovar",
                    plain_language="aprovar ou rejeitar",
                    risk="baixo",
                    reply_instruction="aprovar",
                )
            },
            "decision",
            OwnerMessageState.DECISION,
            False,
        ),
        (
            render_owner_blocked,
            {"reason": "o monitor está aguardando sinal", "next_step": "confirmar o evento externo"},
            "watchdog",
            OwnerMessageState.BLOCKED,
            False,
        ),
        (
            render_owner_completed,
            {"result": "fluxo restaurado", "validation": "smoke real passou"},
            "final",
            OwnerMessageState.COMPLETED,
            True,
        ),
        (
            render_owner_failed,
            {"reason": "validação falhou", "next_step": "corrigir a causa"},
            "failure",
            OwnerMessageState.FAILED,
            True,
        ),
        (
            render_owner_duplicate,
            {"reason": "já existe uma resposta igual", "next_step": "continuar a conversa original"},
            "duplicate",
            OwnerMessageState.BLOCKED,
            False,
        ),
    ],
)
def test_renderers_match_the_canonical_fixture_and_terminal_flags(
    renderer,
    kwargs: dict[str, Any],
    kind: str,
    state: OwnerMessageState,
    terminal: bool,
) -> None:
    case = next(case for case in load_cases() if cast(str, case["kind"]) == kind)

    rendered = renderer(**kwargs)

    assert rendered.state == state
    assert rendered.terminal is terminal
    assert rendered.markdown == cast(str, case["message"])
    assert len(rendered.markdown.splitlines()) <= cast(int, case["max_lines"])
    assert "fingerprint" not in rendered.markdown.lower()
    assert not rendered.markdown.startswith("#")


@pytest.mark.parametrize(
    ("renderer", "kwargs", "field"),
    [
        (render_owner_progress, {"now": "", "next_milestone": "causa confirmada ou bloqueio"}, "now"),
        (render_owner_progress, {"now": "   ", "next_milestone": "causa confirmada ou bloqueio"}, "now"),
        (render_owner_progress, {"now": "validando a causa", "next_milestone": ""}, "next_milestone"),
        (render_owner_progress, {"now": "validando a causa", "next_milestone": "   "}, "next_milestone"),
        (render_owner_next_step, {"now": "", "next_milestone": "executar no horário combinado"}, "now"),
        (render_owner_next_step, {"now": "   ", "next_milestone": "executar no horário combinado"}, "now"),
        (render_owner_next_step, {"now": "agendamento confirmado", "next_milestone": ""}, "next_milestone"),
        (render_owner_next_step, {"now": "agendamento confirmado", "next_milestone": "   "}, "next_milestone"),
        (render_owner_blocked, {"reason": "", "next_step": "confirmar o evento externo"}, "reason"),
        (render_owner_blocked, {"reason": "   ", "next_step": "confirmar o evento externo"}, "reason"),
        (render_owner_blocked, {"reason": "o monitor está aguardando sinal", "next_step": ""}, "next_step"),
        (render_owner_blocked, {"reason": "o monitor está aguardando sinal", "next_step": "   "}, "next_step"),
        (render_owner_duplicate, {"reason": "", "next_step": "continuar a conversa original"}, "reason"),
        (render_owner_duplicate, {"reason": "   ", "next_step": "continuar a conversa original"}, "reason"),
        (render_owner_duplicate, {"reason": "já existe uma resposta igual", "next_step": ""}, "next_step"),
        (render_owner_duplicate, {"reason": "já existe uma resposta igual", "next_step": "   "}, "next_step"),
        (render_owner_completed, {"result": "", "validation": "smoke real passou"}, "result"),
        (render_owner_completed, {"result": "   ", "validation": "smoke real passou"}, "result"),
        (render_owner_completed, {"result": "fluxo restaurado", "validation": ""}, "validation"),
        (render_owner_completed, {"result": "fluxo restaurado", "validation": "   "}, "validation"),
        (render_owner_failed, {"reason": "", "next_step": "corrigir a causa"}, "reason"),
        (render_owner_failed, {"reason": "   ", "next_step": "corrigir a causa"}, "reason"),
        (render_owner_failed, {"reason": "validação falhou", "next_step": ""}, "next_step"),
        (render_owner_failed, {"reason": "validação falhou", "next_step": "   "}, "next_step"),
    ],
)
def test_renderers_reject_empty_or_whitespace_owner_facing_fields(
    renderer,
    kwargs: dict[str, Any],
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        renderer(**kwargs)


def test_render_owner_completed_normalizes_whitespace_pending_to_default_nada() -> None:
    rendered = render_owner_completed(
        result="fluxo restaurado",
        validation="smoke real passou",
        pending="   ",
    )

    assert rendered.state is OwnerMessageState.COMPLETED
    assert rendered.terminal is True
    assert rendered.markdown == "**Concluído e validado**\n\n**Resultado:** fluxo restaurado.\n\n**Validado:** smoke real passou.\n\n**Pendente:** nada."


def test_render_owner_decision_preserves_optional_technical_url_without_breaking_the_linter() -> None:
    prompt = OwnerDecisionPrompt(
        decision="aprovar",
        plain_language="aprovar ou rejeitar",
        risk="baixo",
        reply_instruction="aprovar",
        technical_url="https://example.com/details",
    )

    rendered = render_owner_decision(prompt=prompt)

    assert rendered.state is OwnerMessageState.DECISION
    assert rendered.terminal is False
    assert prompt.technical_url in rendered.markdown
    assert "[cockpit-" not in rendered.markdown
    assert "#" not in rendered.markdown
