"""Codex-shaped ACP review handoff for completed native plans."""

from __future__ import annotations

import logging
import re
from itertools import count
from typing import Any

logger = logging.getLogger(__name__)

_PLAN_REVIEW_IDS = count(1)
_PROPOSED_PLAN_RE = re.compile(
    r"\A\s*<proposed_plan>\s*(?P<body>.+?)\s*</proposed_plan>\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def native_plan_is_active(session_id: str) -> bool:
    """Return whether the stable ACP session is currently planning."""
    try:
        from hermes_cli.plan_mode import PlanModeManager

        state = PlanModeManager(session_id).state
        return state.planning and not state.build_pending
    except Exception:
        logger.debug("Could not inspect native Plan Mode for ACP review", exc_info=True)
        return False


def extract_plan_review_text(response: Any) -> str | None:
    """Extract a complete plan body while rejecting surrounding chatter."""
    match = _PROPOSED_PLAN_RE.fullmatch(str(response or "").strip())
    if not match:
        return None
    body = str(match.group("body") or "").strip()
    return body or None


def _build_plan_review_tool_call(plan_text: str):
    import acp

    tool_call_id = f"plan-review-{next(_PLAN_REVIEW_IDS)}"
    return acp.update_tool_call(
        tool_call_id,
        title="Implement this plan?",
        kind="switch_mode",
        status="pending",
        content=[acp.tool_content(acp.text_block(plan_text))],
        raw_input={"plan": plan_text},
    )


async def request_plan_review(conn: Any, session_id: str, plan_text: str) -> bool:
    """Ask the ACP client to approve implementation of the completed plan."""
    from acp.schema import AllowedOutcome, PermissionOption

    options = [
        PermissionOption(
            option_id="implement",
            kind="allow_once",
            name="Implement plan",
        ),
        PermissionOption(
            option_id="keep_planning",
            kind="reject_once",
            name="Keep planning",
        ),
    ]
    response = await conn.request_permission(
        session_id=session_id,
        tool_call=_build_plan_review_tool_call(plan_text),
        options=options,
    )
    outcome = getattr(response, "outcome", None)
    if isinstance(outcome, AllowedOutcome):
        return outcome.option_id == "implement"
    # Compatibility with simple ACP test doubles and older SDK wrappers.
    return (
        getattr(outcome, "outcome", None) == "selected"
        and getattr(outcome, "option_id", None) == "implement"
    )
