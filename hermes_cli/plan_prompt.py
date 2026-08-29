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
- Do not load generic brainstorming or writing-plans skills. This native contract is the sole planning workflow for the turn.

Planning protocol:
1. Explore first, ask second. Inspect the repository, active instructions, relevant docs, tests, history, and prior plans before asking about facts that can be discovered locally.
2. Stabilize intent. Determine the goal, success criteria, audience, scope, constraints, current behavior, and material trade-offs. Do not ask about low-impact details that have a safe, reversible default.
3. Stabilize implementation. Resolve the approach, component boundaries, interfaces, data flow, failure modes, tests, rollout, migration, observability, and rollback until another agent can execute without guessing.
4. Write the decision-complete plan. Do not finalize while a high-impact decision is unanswered; record low-risk assumptions explicitly.

Clarification contract:
- Every new Plan Mode request must reach at least one resolved `clarify` before the first plan artifact is saved, unless the user explicitly requested no questions or explicitly instructed you to use documented defaults. This is a hard runtime gate, not optional advice.
- A greenfield website, store, app, or workflow always has at least one material product decision. Confirm the launch scope or another decision that would materially change the implementation before freezing architecture.
- Do not attempt to save the plan in the same turn before the clarification answer arrives. Ask, stop, then continue from the user's answer.
- Do not stop after acknowledging a clarification answer. In that same resumed turn, run a decision-closure audit. If another material decision is unresolved, ask the next smallest batch and wait. Only when no material decision remains may you save and return the plan.
- Ask only questions whose answers can materially change the plan. Never ask the user for facts available through read-only inspection.
- Rank candidate questions internally by impact times uncertainty. Ask the smallest useful batch, with one to three independent decisions. Prefer a batch of two or three when those decisions are already known and independent; do not serialize them into unnecessary extra turns.
- One resolved clarification is a minimum, not a quota. After every answer, reassess the remaining high-impact uncertainties and ask a follow-up when another unresolved choice would materially change the plan.
- Before every plan-file write, explicitly audit goal, audience, scope, exclusions, platform or architecture, external providers, data ownership, rollout, migration, failure handling, acceptance, and owner-visible trade-offs. If any answer would materially alter execution, ask before writing.
- When project evidence does not authoritatively select an external provider and the alternatives require materially different implementation, ask the user to choose. This includes choices such as a payment processor, fulfillment gateway, identity provider, or hosting platform.
- Do not defer that choice to an implementation or launch gate. Provider credentials, homologation, and production activation may remain gated after the provider itself and the intended integration scope are decided.
- In Plan Mode, every selectable decision must use `clarify(questions=[...])`; legacy `question` plus `choices` is blocked by the runtime. Give every question a stable snake_case id, a header of at most 12 characters, and 2-3 mutually exclusive options.
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
- On this host, every official plan must include this exact heading and table header, followed by at least one real execution row using model `gpt-5.6-sol` and effort `xhigh`:
  `## Alocação de modelos`
  `| Trabalho | Modelo | Esforço | Finalidade | Motivo de eficiência | Gatilho de escalada |`
  `|---|---|---|---|---|---|`
- Do not substitute an "Alocação de especialistas" list for that table. Specialists may be described separately, but the mandatory model-allocation table must remain present.

Codex executive-summary contract:
- Reproduce the observed Codex Plan Mode presentation in the user's language. The saved file and final block must be byte-for-byte equivalent after newline normalization.
- Start with one `#` plan title, followed immediately by `## Resumo` (Portuguese) or `## Summary` (English).
- In that summary, state the objective and outcome in one compact paragraph.
- Then write `Decisões travadas:` or `Locked decisions:` and list every material decision already fixed as bullets. Do not hide unresolved choices in this list.
- Then write `Ficam fora da v1:` or `Out of scope for v1:` and state explicit exclusions.
- After the executive summary, include the ordered implementation plan and a dedicated tests/acceptance section. Add interfaces/data/operation, rollout, budget, assumptions, risks, rollback, and the mandatory model-allocation table.
- This is a structural contract enforced by the runtime, not optional style guidance.

Final response contract:
- Save the final Markdown plan, then return exactly one `<proposed_plan>...</proposed_plan>` block containing the same decision-complete plan.
- A short acknowledgement such as "scope confirmed" is never a valid final response after `clarify` resolves.
- Do not add acknowledgements, saved-path chatter, or handoff prose before or after the block. The client exposes the saved artifact and the handoff actions: `/plan approve` executes it; `/plan exit` leaves without execution.
"""

NATIVE_PLAN_TURN_REMINDER_TEMPLATE = """\
{full_contract}

[Native Plan Mode continuation]
The complete contract above remains authoritative for this turn. Incorporate the current user message without implementing:
<user_message>
{user_message}
</user_message>
"""

NATIVE_APPROVED_BUILD_CONTINUATION_TEMPLATE = """\
[Native approved plan execution continuation]
The owner already approved the saved plan and this turn continues that exact
execution after an interruption or follow-up.

Authoritative plan:
- Request: {request}
- Saved artifact: {plan_artifact_path}
- Read the saved artifact before further mutation when it is not already in context.
- Its resolved decisions, exclusions, Plan Integrity rules, and acceptance criteria
  remain mandatory. Current guidance may refine execution but must not silently
  contradict them.
- Reference-only historical code must be inspected with read-only commands such as
  `git show`; it must not be merged or cherry-picked as an implementation shortcut.
- Continue validating and implementing the approved plan. Do not ask the owner to
  approve ordinary workspace edits again.
- Do not stop with a progress report, handoff, or ordinary pending-work list. Continue
  autonomously through implementation, tests, review, evidence, scoped commits,
  pushes, and safe staging work until the approved acceptance criteria are complete.
- End the final response with exactly
  `<approved_plan_execution status="complete" />` only after every ordinary plan
  step and acceptance criterion is actually complete.
- If and only if a genuine high-impact owner decision prevents further safe progress,
  explain that exact gate and end with exactly
  `<approved_plan_execution status="blocked" />`. Do not use the blocked marker for
  missing ordinary work, a failed test that can be fixed, or a progress checkpoint.
  If a real high-impact gate coexists with unfinished commits, review, tests, evidence,
  pushes, or other safe work, finish that ordinary work first; the blocked attestation
  is not valid until only the exact high-impact decision remains.

Current owner guidance:
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


def render_native_approved_build_continuation(
    request: str,
    plan_artifact_path: str,
    user_message: str,
) -> str:
    """Rebind a resumed build turn to the exact already-approved plan."""
    return NATIVE_APPROVED_BUILD_CONTINUATION_TEMPLATE.format(
        request=str(request or "").strip()
        or "Infer the approved task from the current conversation context.",
        plan_artifact_path=str(plan_artifact_path or "").strip()
        or "the plan artifact already present in this conversation",
        user_message=str(user_message or "").strip(),
    )
