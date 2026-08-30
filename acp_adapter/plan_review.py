"""Render completed native plans as plain Markdown on ACP clients.

Execution approval deliberately remains an explicit ``/plan approve`` command.
ACP permission cards use the generic Allow/Always/Deny semantics and therefore
cannot represent the nonce-bound Plan Mode handoff without creating a second,
ambiguous approval path.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

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
