from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

MAX_RELAY_CHARS = 1200
_LINK_LABEL = "Abrir detalhes técnicos"

_MARKER_LINE = re.compile(r"(?m)^\[(?:cockpit-[^\]]+|cockpit:[^\]]+)\]\s*$")
_GATE_MARKER_LINE = re.compile(r"^\[cockpit-gate:[^\]]+\]\s*$")
_INLINE_MARKER = re.compile(r"\[(?:cockpit-[^\]]+|cockpit:[^\]]+)\]")
_COCKPIT_TOKEN = re.compile(r"(?i)\bcockpit[-:][a-z0-9][a-z0-9_.:-]*\b")
_LONG_INTERNAL_ID = re.compile(r"\b[a-z0-9]{26}\b")
_RAW_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?")
_RAW_HTTP = re.compile(
    r"(?i)\b(?:HTTP(?:/\d(?:\.\d)?)?\s*[1-5]\d{2}|status\s*=?\s*[1-5]\d{2}|[1-5]\d{2}\s+[A-Z][A-Za-z-]*(?:\s+[A-Z][A-Za-z-]*)*)\b"
)
_ROUTINE = re.compile(
    r"(?im)^[^\w\n]*(?:"
    r"interrupting(?: current task)?|"
    r"working(?:\.\.\.)?|"
    r"watcher(?: heartbeat)?|"
    r"wake|heartbeat|"
    r"compression(?:\s+(?:started|working(?:\.\.\.)?))?|"
    r"context compaction"
    r")(?:\b|\W)"
)
_HELPER_PREAMBLE = re.compile(r"(?im)^##\s+\d+\s+new posts?\b")
_SHELL_LINE = re.compile(r"(?im)(?:^\s*\$\s+\S+|\bhermes-mattermost-cockpit\b)")
_GATE_TOKEN = re.compile(r"(?i)\bgate[-_:][a-z0-9_.:-]+\b")
_AUTH_MATERIAL = re.compile(r"(?i)\b(?:authorization\s*:\s*bearer|bearer\s+\S+)\b")
_VISIBLE_URL = re.compile(r"(?i)(?:https?://|\[[^\]]+\]\([^\)]+\))")
_BOLD_HEADING = re.compile(r"(?m)^\*\*[^*\n]+\*\*\s*$")
_ATX_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
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
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        raise ValueError("execution permalink must be an absolute HTTP URL")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in "[]()" for ch in value):
        raise ValueError("execution permalink must be an absolute HTTP URL")
    return value


def _plain(value: str, *, field: str) -> str:
    value = value.replace("\r", "\n").replace("—", ", ")
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
        (_BOLD_HEADING, "an unsupported heading"),
        (_ATX_HEADING, "an unsupported heading"),
        (_INLINE_MARKER, "a cockpit marker"),
        (_COCKPIT_TOKEN, "a cockpit token"),
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
    if len(link) > MAX_RELAY_CHARS:
        raise ValueError("execution permalink is too long for the owner relay contract")
    structured: list[tuple[str, str]] = []
    for section in sections:
        heading, separator, content = section.partition("\n")
        if not separator or not heading.startswith("**") or not heading.endswith("**"):
            structured = []
            break
        structured.append((heading, content))

    if structured:
        separator_chars = 2 * len(structured)
        fixed_chars = sum(len(heading) + 1 for heading, _ in structured) + separator_chars + len(link)
        if fixed_chars <= MAX_RELAY_CHARS:
            body_budget = MAX_RELAY_CHARS - fixed_chars
            total_content = sum(len(content) for _, content in structured)
            rendered_sections: list[str] = []
            if total_content <= body_budget:
                rendered_sections = [f"{heading}\n{content}" for heading, content in structured]
            else:
                content_budget = max(0, body_budget - len(structured))
                count = len(structured)
                base = content_budget // count
                extra = content_budget % count
                for index, (heading, content) in enumerate(structured):
                    quota = base + (1 if index < extra else 0)
                    if quota <= 0:
                        rendered_content = ""
                    else:
                        rendered_content = content if len(content) <= quota else _truncate_text(content, quota)
                    rendered_sections.append(f"{heading}\n{rendered_content}")
            body = "\n\n".join(rendered_sections + [link])
            if len(body) <= MAX_RELAY_CHARS:
                return body

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


def _semantic_update(message: str) -> tuple[str, str] | None:
    lines = message.replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped not in {"[cockpit-owner-state]", "[cockpit-owner-blocked]"}:
            return None
        return stripped, "\n".join(lines[index + 1 :])
    return None


def _sections(prompt: str) -> tuple[str, str]:
    lines = prompt.replace("\r", "\n").split("\n")
    non_empty = [index for index, line in enumerate(lines) if line.strip()]
    if not non_empty:
        raise ValueError("gate prompt must not be empty")

    marker_indexes = [index for index in non_empty if _MARKER_LINE.fullmatch(lines[index].strip())]
    if marker_indexes:
        if (
            marker_indexes != [non_empty[0]]
            or len(marker_indexes) != 1
            or not _GATE_MARKER_LINE.fullmatch(lines[marker_indexes[0]].strip())
        ):
            raise ValueError("gate prompt marker must be the first non-empty line")
        del lines[marker_indexes[0]]

    if any(_MARKER_LINE.fullmatch(line.strip()) for line in lines if line.strip()):
        raise ValueError("gate prompt contains an internal marker")

    first_non_empty = next((line.strip() for line in lines if line.strip()), "")
    if first_non_empty != "**Bloqueado**":
        raise ValueError("gate prompt requires exactly one blocker and exactly one decision")

    prompt = "\n".join(lines)
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
    semantic = _semantic_update(message)
    if semantic is None:
        return None
    marker, tail = semantic
    if marker == "[cockpit-owner-blocked]":
        try:
            return render_gate(tail, permalink)
        except ValueError:
            return None
    try:
        state = _plain(tail, field="state")
    except ValueError:
        return None
    return RenderedRelay(RelayKind.UPDATE, _bounded([f"**Em andamento**\n{state}"], permalink))
