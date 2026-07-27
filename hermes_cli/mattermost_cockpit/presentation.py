from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

__all__ = [
    "OwnerDecisionPrompt",
    "OwnerMessageState",
    "RenderedOwnerMessage",
    "render_owner_blocked",
    "render_owner_completed",
    "render_owner_decision",
    "render_owner_duplicate",
    "render_owner_failed",
    "render_owner_next_step",
    "render_owner_progress",
    "validate_owner_decision_prompt",
    "validate_owner_markdown",
]

_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
_INTERNAL_MARKER_RE = re.compile(r"\[cockpit-[^]]+]")
_FINGERPRINT_RE = re.compile(r"\bfingerprint\b", re.IGNORECASE)


class OwnerMessageState(StrEnum):
    PROGRESS = "progress"
    NEXT_STEP = "next_step"
    DECISION = "decision"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class RenderedOwnerMessage:
    state: OwnerMessageState
    terminal: bool
    markdown: str


@dataclass(frozen=True)
class OwnerDecisionPrompt:
    decision: str
    plain_language: str
    risk: str
    reply_instruction: str
    technical_url: str | None = None


def _required_text(label: str, text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _sentence(text: str) -> str:
    value = text.strip()
    if not value:
        return value
    if value.endswith((".", "!", "?")):
        return value
    return f"{value}."


def validate_owner_markdown(message: str, *, max_lines: int = 24) -> None:
    if _HEADING_RE.search(message):
        raise ValueError("owner-facing Mattermost messages cannot use headings")
    if _INTERNAL_MARKER_RE.search(message):
        raise ValueError("internal markers must live in post props")
    if _FINGERPRINT_RE.search(message):
        raise ValueError("technical terms must not be visible in owner-facing text")
    if len(message.splitlines()) > max_lines:
        raise ValueError("owner-facing Mattermost message exceeds line budget")


def validate_owner_decision_prompt(prompt: OwnerDecisionPrompt) -> None:
    _required_text("plain_language", prompt.plain_language)
    _required_text("risk", prompt.risk)
    _required_text("reply_instruction", prompt.reply_instruction)


def _render(
    state: OwnerMessageState,
    *,
    terminal: bool,
    parts: list[str],
    max_lines: int,
) -> RenderedOwnerMessage:
    if terminal != (state in {OwnerMessageState.COMPLETED, OwnerMessageState.FAILED}):
        raise ValueError("terminal flag does not match owner message state")
    markdown = "\n\n".join(part.strip() for part in parts if part.strip())
    validate_owner_markdown(markdown, max_lines=max_lines)
    return RenderedOwnerMessage(state=state, terminal=terminal, markdown=markdown)


def render_owner_progress(*, now: str, next_milestone: str) -> RenderedOwnerMessage:
    now_text = _required_text("now", now)
    next_milestone_text = _required_text("next_milestone", next_milestone)
    return _render(
        OwnerMessageState.PROGRESS,
        terminal=False,
        parts=[
            "**Em andamento, ainda não concluído**",
            f"**Agora:** {_sentence(now_text)}",
            f"**Próximo marco:** {_sentence(next_milestone_text)}",
        ],
        max_lines=8,
    )


def render_owner_next_step(*, now: str, next_milestone: str) -> RenderedOwnerMessage:
    now_text = _required_text("now", now)
    next_milestone_text = _required_text("next_milestone", next_milestone)
    return _render(
        OwnerMessageState.NEXT_STEP,
        terminal=False,
        parts=[
            "**Próximo passo, ainda não concluído**",
            f"**Agora:** {_sentence(now_text)}",
            f"**Próximo marco:** {_sentence(next_milestone_text)}",
        ],
        max_lines=8,
    )


def render_owner_decision(*, prompt: OwnerDecisionPrompt) -> RenderedOwnerMessage:
    validate_owner_decision_prompt(prompt)

    parts = [
        "**Preciso de uma decisão sua**",
        f"**Em linguagem simples:** {_sentence(prompt.plain_language)}",
        f"**Risco:** {_sentence(prompt.risk)}",
        f"**Como responder:** `{prompt.reply_instruction.strip()}`.",
    ]
    technical_url = prompt.technical_url.strip() if prompt.technical_url else ""
    if technical_url:
        parts.append(f"[Ver detalhes técnicos]({technical_url})")
    return _render(
        OwnerMessageState.DECISION,
        terminal=False,
        parts=parts,
        max_lines=16,
    )


def render_owner_blocked(*, reason: str, next_step: str) -> RenderedOwnerMessage:
    reason_text = _required_text("reason", reason)
    next_step_text = _required_text("next_step", next_step)
    return _render(
        OwnerMessageState.BLOCKED,
        terminal=False,
        parts=[
            "**Bloqueado, ainda não concluído**",
            f"**Agora:** {_sentence(reason_text)}",
            f"**Próximo passo:** {_sentence(next_step_text)}",
        ],
        max_lines=8,
    )


def render_owner_duplicate(*, reason: str, next_step: str) -> RenderedOwnerMessage:
    return render_owner_blocked(reason=reason, next_step=next_step)


def render_owner_completed(*, result: str, validation: str, pending: str = "nada") -> RenderedOwnerMessage:
    result_text = _required_text("result", result)
    validation_text = _required_text("validation", validation)
    pending_text = pending.strip() or "nada"
    return _render(
        OwnerMessageState.COMPLETED,
        terminal=True,
        parts=[
            "**Concluído e validado**",
            f"**Resultado:** {_sentence(result_text)}",
            f"**Validado:** {_sentence(validation_text)}",
            f"**Pendente:** {_sentence(pending_text)}",
        ],
        max_lines=10,
    )


def render_owner_failed(*, reason: str, next_step: str) -> RenderedOwnerMessage:
    reason_text = _required_text("reason", reason)
    next_step_text = _required_text("next_step", next_step)
    return _render(
        OwnerMessageState.FAILED,
        terminal=True,
        parts=[
            "**Não concluído**",
            f"**Motivo:** {_sentence(reason_text)}",
            f"**Próximo passo:** {_sentence(next_step_text)}",
        ],
        max_lines=8,
    )
