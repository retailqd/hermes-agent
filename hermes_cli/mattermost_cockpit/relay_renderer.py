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
    r"(?im)^(?:interrupting current task|working|watcher|wake|heartbeat|compression|context compaction)(?:\b|\W)"
)
_HELPER_PREAMBLE = re.compile(r"(?im)^##\s+\d+\s+new posts?\b")
_SHELL_LINE = re.compile(r"(?m)^\s*(?:\$\s+|hermes-mattermost-cockpit\s+)")
_GATE_TOKEN = re.compile(r"(?i)\bgate[-_:][a-z0-9_.:-]+\b")
_AUTH_MATERIAL = re.compile(r"(?i)\b(?:authorization\s*:\s*bearer|bearer\s+\S+)\b")
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


@dataclass(frozen=True, slots=True)
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


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    clipped = value[:limit].rsplit(" ", 1)[0].rstrip(" .,:;")
    return f"{clipped or value[:max(0, limit - 1)]}…"


def _bounded(sections: list[str], permalink: str) -> str:
    link = f"[{_LINK_LABEL}]({_permalink(permalink)})"
    budget = MAX_RELAY_CHARS - len(link) - 2
    kept: list[str] = []
    for section in sections:
        separator_chars = 2 if kept else 0
        used = sum(len(item) for item in kept) + separator_chars * len(kept)
        available = budget - used
        if available <= 0:
            break
        next_section = section if len(section) <= available else _truncate_text(section, available)
        kept.append(next_section)
        if len(next_section) < len(section):
            break
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
        return RenderedRelay(RelayKind.UPDATE, _bounded([f"**Em andamento**\n{state}"], permalink))
    return None
