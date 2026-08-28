#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
import re
from typing import Any, Callable, Dict, List, Optional


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4
MAX_STRUCTURED_QUESTIONS = 3
MIN_STRUCTURED_OPTIONS = 2
MAX_STRUCTURED_OPTIONS = 3
MAX_HEADER_LENGTH = 12
_QUESTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_OTHER_LABELS = frozenset({"other", "other (type your answer)", "outro", "outra"})
_RECOMMENDED_SUFFIX_RE = re.compile(r"\s*\(Recommended\)\s*$", re.IGNORECASE)


def _base_option_label(label: str) -> str:
    return _RECOMMENDED_SUFFIX_RE.sub("", label).strip().casefold()


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def clarify_tool(
    question: str = "",
    choices: Optional[List[str]] = None,
    questions: Optional[List[Dict[str, Any]]] = None,
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present (legacy single-question mode).
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        questions: One to three structured questions. Each question carries a
                   stable id, short header, prompt, and two or three options
                   with labels and consequence descriptions. This mode is
                   validated before any UI interaction and is presented
                   sequentially through the existing callback contract.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner (cli.py / gateway).

    Returns:
        JSON string with the user's response.
    """
    if questions is not None:
        if str(question or "").strip() or choices is not None:
            return tool_error("Use either `question`/`choices` or `questions`, not both.")
        return _run_structured_questions(questions, callback)

    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # LLMs sometimes emit dict-shaped choices (e.g. [{"description": "..."}])
        # instead of bare strings. _flatten_choice unwraps them to their
        # user-facing text here — the single platform-agnostic entry point —
        # so the CLI panel, Discord buttons, and Telegram list all render clean
        # text and the resolved answer is never a raw Python dict repr.
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
    }, ensure_ascii=False)


def _validate_structured_questions(
    questions: Any,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate the strict, Codex-compatible batch contract atomically."""
    if not isinstance(questions, list):
        return None, "questions must be a list."
    if not 1 <= len(questions) <= MAX_STRUCTURED_QUESTIONS:
        return None, "questions must contain between 1 and 3 items."

    normalized: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_question in enumerate(questions, start=1):
        if not isinstance(raw_question, dict):
            return None, f"questions[{index - 1}] must be an object."

        question_id = str(raw_question.get("id") or "").strip()
        header = str(raw_question.get("header") or "").strip()
        prompt = str(raw_question.get("question") or "").strip()
        options = raw_question.get("options")

        if not _QUESTION_ID_RE.fullmatch(question_id):
            return None, (
                f"Question {index} id must be unique snake_case beginning with a letter."
            )
        if question_id in seen_ids:
            return None, f"Question id `{question_id}` is duplicated."
        seen_ids.add(question_id)
        if not header or len(header) > MAX_HEADER_LENGTH:
            return None, f"Question `{question_id}` header must contain 1 to 12 characters."
        if not prompt:
            return None, f"Question `{question_id}` text is required."
        if not isinstance(options, list) or not (
            MIN_STRUCTURED_OPTIONS <= len(options) <= MAX_STRUCTURED_OPTIONS
        ):
            return None, f"Question `{question_id}` must provide 2 or 3 options."

        normalized_options: List[Dict[str, str]] = []
        seen_labels: set[str] = set()
        for option_index, raw_option in enumerate(options):
            if not isinstance(raw_option, dict):
                return None, f"Question `{question_id}` option {option_index + 1} must be an object."
            label = str(raw_option.get("label") or "").strip()
            description = str(raw_option.get("description") or "").strip()
            if not label or not description:
                return None, (
                    f"Question `{question_id}` option {option_index + 1} requires label and description."
                )
            label_key = label.casefold()
            base_label = _base_option_label(label)
            if not base_label:
                return None, (
                    f"Question `{question_id}` option {option_index + 1} requires a label before `(Recommended)`."
                )
            if len(_RECOMMENDED_SUFFIX_RE.sub("", label).split()) > 5:
                return None, (
                    f"Question `{question_id}` option {option_index + 1} label must contain at most 5 words."
                )
            if label_key in seen_labels:
                return None, f"Question `{question_id}` option labels must be unique."
            if label_key in _OTHER_LABELS or base_label in _OTHER_LABELS:
                return None, (
                    f"Question `{question_id}` must not define Other; clients add it automatically."
                )
            seen_labels.add(label_key)
            normalized_options.append({"label": label, "description": description})

        if not normalized_options[0]["label"].endswith("(Recommended)"):
            return None, (
                f"Question `{question_id}` first option must end with `(Recommended)`."
            )
        if any(
            option["label"].endswith("(Recommended)")
            for option in normalized_options[1:]
        ):
            return None, (
                f"Question `{question_id}` may mark only its first option as recommended."
            )

        normalized.append(
            {
                "id": question_id,
                "header": header,
                "question": prompt,
                "options": normalized_options,
            }
        )
    return normalized, None


def _run_structured_questions(
    questions: Any,
    callback: Optional[Callable],
) -> str:
    normalized, validation_error = _validate_structured_questions(questions)
    if validation_error:
        return tool_error(validation_error)
    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    responses = []
    answers: Dict[str, Dict[str, List[str]]] = {}
    assert normalized is not None
    total = len(normalized)
    for index, item in enumerate(normalized, start=1):
        progress = f" · {index}/{total}" if total > 1 else ""
        option_details = "\n".join(
            f"- {option['label']}: {option['description']}"
            for option in item["options"]
        )
        display_question = (
            f"[{item['header']}{progress}] {item['question']}\n\n"
            f"Trade-offs:\n{option_details}"
        )
        labels = [option["label"] for option in item["options"]]
        try:
            raw_response = callback(display_question, labels)
        except Exception as exc:
            return json.dumps(
                {
                    "error": f"Failed to get user input: {exc}",
                    "completed_responses": responses,
                },
                ensure_ascii=False,
            )
        response = str(raw_response or "").strip()
        if not response:
            return json.dumps(
                {
                    "error": f"Question `{item['id']}` was dismissed or timed out.",
                    "completed_responses": responses,
                },
                ensure_ascii=False,
            )
        selected_label = response if response in labels else None
        record = {
            "id": item["id"],
            "header": item["header"],
            "response": response,
            "selected_label": selected_label,
        }
        responses.append(record)
        answers[item["id"]] = {"answers": [response]}

    return json.dumps(
        {"answers": answers, "responses": responses},
        ensure_ascii=False,
    )


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports three modes:\n\n"
        "1. **Structured decision batch (preferred for planning)** — pass "
        "`questions` with 1 to 3 independent decisions. Each has a stable "
        "snake_case id, a header of at most 12 characters, and 2 or 3 options. "
        "Put the recommended option first and suffix its label with "
        "`(Recommended)`. Give every option a concise consequence in "
        "`description`; clients add a free-form Other choice automatically.\n"
        "2. **Multiple choice (legacy)** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "3. **Open-ended (legacy)** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STRUCTURED_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_]*$",
                            "description": "Stable snake_case answer identifier.",
                        },
                        "header": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_HEADER_LENGTH,
                            "description": "Short label shown above the question.",
                        },
                        "question": {
                            "type": "string",
                            "minLength": 1,
                            "description": "One material decision, phrased as a question.",
                        },
                        "options": {
                            "type": "array",
                            "minItems": MIN_STRUCTURED_OPTIONS,
                            "maxItems": MAX_STRUCTURED_OPTIONS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "minLength": 1},
                                    "description": {"type": "string", "minLength": 1},
                                },
                                "required": ["label", "description"],
                            },
                        },
                    },
                    "required": ["id", "header", "question", "options"],
                },
                "description": (
                    "Preferred planning form. Supply 1-3 independent questions. "
                    "The first option is the recommendation and its label must "
                    "end with `(Recommended)`. Do not add an Other option."
                ),
            },
        },
        "description": (
            "Use exactly one form: either `questions`, or the legacy `question` "
            "with optional `choices`. Runtime validation enforces the contract."
        ),
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        questions=args.get("questions"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
