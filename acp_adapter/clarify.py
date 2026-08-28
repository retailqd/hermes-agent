"""Bridge Hermes clarifying questions to ACP's native selection UI."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FutureTimeout
from itertools import count
from typing import Callable, Sequence

from acp.schema import AllowedOutcome, PermissionOption

logger = logging.getLogger(__name__)
_REQUEST_IDS = count(1)


def _build_clarify_tool_call(question: str, choices: Sequence[str]):
    import acp

    return acp.update_tool_call(
        f"clarify-{next(_REQUEST_IDS)}",
        title=question,
        kind="think",
        status="pending",
        content=[acp.tool_content(acp.text_block(question))],
        raw_input={"question": question, "choices": list(choices)},
    )


def make_acp_clarify_callback(
    request_permission_fn: Callable,
    loop: asyncio.AbstractEventLoop,
    session_id: str,
    timeout: float = 300.0,
) -> Callable[[str, Sequence[str] | None], str]:
    """Return a synchronous clarify callback backed by ACP request_permission.

    ACP currently exposes selectable permission options but no portable free-text
    input request. Open-ended questions therefore fail closed instead of hanging
    or fabricating a response.
    """

    def _callback(question: str, choices: Sequence[str] | None = None) -> str:
        from agent.async_utils import safe_schedule_threadsafe

        normalized = [str(choice).strip() for choice in (choices or []) if str(choice).strip()]
        if not normalized:
            raise RuntimeError(
                "ACP supports selectable clarification choices only; ask with choices."
            )

        options = [
            PermissionOption(
                option_id=f"choice_{index}",
                kind="allow_once",
                name=choice,
            )
            for index, choice in enumerate(normalized)
        ]
        options.append(
            PermissionOption(option_id="cancel", kind="reject_once", name="Cancel")
        )
        tool_call = _build_clarify_tool_call(question, normalized)
        coro = request_permission_fn(
            session_id=session_id,
            tool_call=tool_call,
            options=options,
        )
        future = safe_schedule_threadsafe(
            coro,
            loop,
            logger=logger,
            log_message="Clarify request: failed to schedule on loop",
        )
        if future is None:
            raise RuntimeError("ACP clarification could not be scheduled.")
        try:
            response = future.result(timeout=timeout)
        except (FutureTimeout, Exception) as exc:
            future.cancel()
            raise RuntimeError("ACP clarification timed out or failed.") from exc

        outcome = getattr(response, "outcome", None)
        if not isinstance(outcome, AllowedOutcome):
            raise RuntimeError("ACP clarification was cancelled.")
        option_id = str(getattr(outcome, "option_id", "") or "")
        if not option_id.startswith("choice_"):
            raise RuntimeError("ACP clarification returned an invalid selection.")
        try:
            index = int(option_id.removeprefix("choice_"))
            return normalized[index]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("ACP clarification returned an invalid selection.") from exc

    return _callback
