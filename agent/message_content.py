from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_NON_TEXT_PART_TYPES = {"image", "image_url", "input_image", "audio", "input_audio"}
_TEXT_KEYS = ("text", "content", "input_text", "output_text", "summary_text")


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _text_from_part(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part

    part_type = str(_field(part, "type") or "").strip().lower()
    if part_type in _NON_TEXT_PART_TYPES:
        return ""

    for key in _TEXT_KEYS:
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def flatten_message_text(content: Any, *, sep: str = "\n") -> str:
    """Return the visible text from common chat/Responses message content shapes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = [_text_from_part(part) for part in content]
        return sep.join(chunk for chunk in chunks if chunk)

    text = _text_from_part(content)
    if text:
        return text
    try:
        return str(content)
    except Exception:
        return ""


def replace_message_text(content: Any, text: str) -> Any:
    """Replace visible text while preserving every non-text content part.

    Multimodal user turns are commonly represented as a list containing one
    text part plus image/audio parts. Callers that need to wrap or rewrite the
    visible text must not stringify that list because doing so turns media into
    enormous base64 prose and destroys the provider-native content shape.
    """
    if not isinstance(content, list):
        return text

    replaced: list[Any] = []
    inserted = False
    for part in content:
        if isinstance(part, str):
            if not inserted:
                replaced.append(text)
                inserted = True
            continue
        if isinstance(part, Mapping):
            part_type = str(part.get("type") or "").strip().lower()
            is_text = part_type in {"text", "input_text", "output_text"} or (
                not part_type and isinstance(part.get("text"), str)
            )
            if is_text:
                if not inserted:
                    updated = dict(part)
                    updated["text"] = text
                    replaced.append(updated)
                    inserted = True
                continue
        replaced.append(part)

    if not inserted:
        replaced.insert(0, {"type": "text", "text": text})
    return replaced
