"""Self-contained prompt contract for native Plan Mode."""

from __future__ import annotations


NATIVE_PLAN_PROMPT_TEMPLATE = """\
[Native Plan Mode is active for this session.]

Task to plan:
<request>
{request}
</request>

Runtime boundary:
- Planning only. Do not implement, mutate project files, run external actions, commit, push, deploy, or send messages.
- Runtime enforcement blocks mutating and unknown tools even in YOLO mode.
- Read-only inspection and material clarification are allowed.
- Save or revise the decision-complete plan only under the active workspace's `.hermes/plans/` directory.

Plan requirements:
- State the goal, current evidence, assumptions, proposed architecture, and acceptance criteria.
- Evidence freshness: record the inspected commit SHA and dirty-tree status. Use symbol anchors instead of line numbers when the inspected tree does not exactly match the target ref.
- Prior project knowledge: search project docs, runbooks, incident notes, previous plans, and relevant session history. State what was found or that nothing relevant was found.
- Order tasks by the first observable owner value, then the next thin slice.
- For code work, include exact paths, target symbols, failing tests, minimal implementation, exact verification commands, expected outcomes, and scoped commits.
- Adversarial premises: explain what breaks if each external assumption changes, how the change is detected, and the cost to undo it.
- Include risks, tradeoffs, rollback, open questions, and a Plan Integrity section.

Handoff:
- End by telling the user to run `/plan approve` to execute the saved plan.
- Tell the user to run `/plan exit` to leave without execution.
"""


def render_native_plan_prompt(request: str) -> str:
    """Render the native planning instructions around the current request."""
    normalized = str(request or "").strip()
    return NATIVE_PLAN_PROMPT_TEMPLATE.format(
        request=normalized
        or "Infer the planning task from the current conversation context."
    )
