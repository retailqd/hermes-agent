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

Planning protocol:
1. Explore first, ask second. Inspect the repository, active instructions, relevant docs, tests, history, and prior plans before asking about facts that can be discovered locally.
2. Stabilize intent. Determine the goal, success criteria, audience, scope, constraints, current behavior, and material trade-offs. Do not ask about low-impact details that have a safe, reversible default.
3. Stabilize implementation. Resolve the approach, component boundaries, interfaces, data flow, failure modes, tests, rollout, migration, observability, and rollback until another agent can execute without guessing.
4. Write the decision-complete plan. Do not finalize while a high-impact decision is unanswered; record low-risk assumptions explicitly.

Clarification contract:
- Ask only questions whose answers can materially change the plan. Never ask the user for facts available through read-only inspection.
- Rank candidate questions internally by impact times uncertainty. Ask the smallest useful set, normally one question; batch at most three independent decisions.
- In Plan Mode, prefer `clarify(questions=[...])`. Give every question a stable snake_case id, a header of at most 12 characters, and 2-3 mutually exclusive options.
- Put the recommended option first, suffix its label with `(Recommended)`, and explain the consequence of every option. The client adds a free-form Other choice automatically.
- Never hide several questions in one prose string. Never repeat a resolved question. Treat a free-form Other response as authoritative and update the plan from it.
- Keep facts, prior-plan proposals, low-risk assumptions, and current user-confirmed decisions distinct. Never relabel an older plan or inferred default as an answer from the user.
- Use a genuinely open-ended legacy `clarify(question=...)` only when useful options cannot be stated without bias.

Plan requirements:
- Write one actionable Markdown implementation plan that another agent can execute without guessing.
- Be decision-complete, not encyclopedic. For a normal feature, target 1,200-2,500 words and exceed that only when explicit requirements or genuine risk demand it. Do not repeat the exploration transcript or resolved questions.
- State the goal, current evidence, assumptions, proposed architecture, and acceptance criteria.
- Evidence freshness: record the inspected commit SHA and dirty-tree status. Use symbol anchors instead of line numbers when the inspected tree does not exactly match the target ref.
- Prior project knowledge: search project docs, runbooks, incident notes, previous plans, and relevant session history. State what was found or that nothing relevant was found.
- Order tasks by the first observable owner value, then the next thin slice.
- For code work, include exact paths, target symbols, failing tests, minimal implementation, exact verification commands, expected outcomes, and scoped commits.
- Adversarial premises: explain what breaks if each external assumption changes, how the change is detected, and the cost to undo it.
- Include risks, tradeoffs, rollback, open questions, and a Plan Integrity section.
- Honor every mandatory plan section in the active `AGENTS.md`, including any required model-allocation contract.

Final response contract:
- Save the final Markdown plan, then return exactly one `<proposed_plan>...</proposed_plan>` block containing the same decision-complete plan.
- Include the saved plan path and the handoff commands inside that block: `/plan approve` executes it; `/plan exit` leaves without execution.
"""

NATIVE_PLAN_TURN_REMINDER_TEMPLATE = """\
{full_contract}

[Native Plan Mode continuation]
The complete contract above remains authoritative for this turn. Incorporate the current user message without implementing:
<user_message>
{user_message}
</user_message>
"""


def render_native_plan_prompt(request: str) -> str:
    """Render the native planning instructions around the current request."""
    normalized = str(request or "").strip()
    return NATIVE_PLAN_PROMPT_TEMPLATE.format(
        request=normalized
        or "Infer the planning task from the current conversation context."
    )


def render_native_plan_turn_reminder(request: str, user_message: str) -> str:
    """Reinject the full contract after resume/compression on every later turn."""
    return NATIVE_PLAN_TURN_REMINDER_TEMPLATE.format(
        full_contract=render_native_plan_prompt(request),
        user_message=str(user_message or "").strip(),
    )
