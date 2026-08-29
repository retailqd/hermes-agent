from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import threading

import pytest

from hermes_cli import plan_mode


@pytest.fixture()
def isolated_plan_mode(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    plan_mode._DB_CACHE.clear()
    yield workspace
    plan_mode._DB_CACHE.clear()


def test_plan_state_is_opt_in_and_persisted(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-1")
    assert not manager.active

    state = manager.activate("refactor the bridge")
    assert state.active
    assert plan_mode.PlanModeManager("session-1").state.request == "refactor the bridge"

    approved = manager.approve()
    assert approved.mode == plan_mode.PLAN_MODE_PLAN
    assert approved.last_action == "approval_pending"
    assert approved.approval_id
    assert plan_mode.PlanModeManager("session-1").active

    started = manager.begin_build(approved.approval_id)
    assert started.mode == plan_mode.PLAN_MODE_BUILD
    assert started.last_action == "build_started"
    assert not plan_mode.PlanModeManager("session-1").active


def test_plan_state_database_opens_runtime_hermes_home(isolated_plan_mode, monkeypatch):
    opened_with = []
    sentinel = object()

    def fake_session_db(db_path, **kwargs):
        opened_with.append((db_path, kwargs))
        return sentinel

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "sessions": {
                "fts_trigram_enabled": False,
                "wal_size_limit_mb": 256,
            }
        },
    )
    monkeypatch.setattr("hermes_state.SessionDB", fake_session_db)

    assert plan_mode._get_session_db() is sentinel
    assert opened_with == [
        (
            Path(os.environ["HERMES_HOME"]) / "state.db",
            {"fts_trigram_enabled": False, "wal_size_limit_mb": 256},
        )
    ]


def test_plan_command_requires_explicit_approval(isolated_plan_mode, monkeypatch):
    monkeypatch.setattr(
        plan_mode, "build_plan_prompt", lambda request, **_: f"PLAN:{request}"
    )

    entered = plan_mode.handle_plan_command("session-2", "add native mode")
    assert entered.action == "enter"
    assert entered.plan_mode == "plan"
    assert entered.prompt == "PLAN:add native mode"

    status = plan_mode.handle_plan_command("session-2", "status")
    assert status.action == "status"
    assert "Mutating tools are blocked" in status.message

    approved = plan_mode.handle_plan_command("session-2", "approve")
    assert approved.action == "approve"
    assert approved.plan_mode == "plan"
    assert approved.prompt == plan_mode.build_plan_execution_prompt(
        plan_mode.PlanModeManager("session-2").state.approval_id
    )
    assert plan_mode.PlanModeManager("session-2").state.build_pending


def test_build_plan_prompt_is_self_contained(monkeypatch):
    def forbidden_skill_loader(*args, **kwargs):
        raise AssertionError("native Plan Mode must not load a skill")

    monkeypatch.setattr(
        "agent.skill_commands.build_skill_invocation_message",
        forbidden_skill_loader,
    )

    prompt = plan_mode.build_plan_prompt(
        "remove the duplicate planning skill",
        task_id="session-native-plan",
    )

    assert "Native Plan Mode is active" in prompt
    assert "Write one actionable Markdown implementation plan" in prompt
    assert "remove the duplicate planning skill" in prompt
    assert ".hermes/plans/" in prompt
    assert "/plan approve" in prompt
    assert "/plan exit" in prompt
    assert "Evidence freshness" in prompt
    assert "Prior project knowledge" in prompt
    assert "Adversarial premises" in prompt
    assert "Explore first, ask second" in prompt
    assert "impact times uncertainty" in prompt
    assert "clarify(questions=[...])" in prompt
    assert "at least one resolved `clarify`" in prompt
    assert "Do not load generic brainstorming or writing-plans skills" in prompt
    assert "<proposed_plan>" in prompt
    assert "Do not stop after acknowledging a clarification answer" in prompt
    assert "One resolved clarification is a minimum, not a quota" in prompt
    assert "decision-closure audit" in prompt
    assert "Before every plan-file write" in prompt
    assert "Codex executive-summary contract" in prompt
    assert "Decisões travadas:" in prompt
    assert "Ficam fora da v1:" in prompt
    assert "byte-for-byte equivalent" in prompt
    assert "external provider" in prompt
    assert "payment processor" in prompt
    assert "Do not defer that choice to an implementation or launch gate" in prompt
    assert "1,200-2,500 words" in prompt
    assert "user has invoked" not in prompt.lower()
    assert "skill content" not in prompt.lower()


def test_plan_command_enters_without_installed_plan_skill(
    isolated_plan_mode, monkeypatch
):
    def forbidden_skill_loader(*args, **kwargs):
        raise AssertionError("native Plan Mode must not load a skill")

    monkeypatch.setattr(
        "agent.skill_commands.build_skill_invocation_message",
        forbidden_skill_loader,
    )

    result = plan_mode.handle_plan_command(
        "session-no-plan-skill",
        "inspect without a skill",
        task_id="session-no-plan-skill",
    )

    assert result.action == "enter"
    assert result.plan_mode == plan_mode.PLAN_MODE_PLAN
    assert result.prompt is not None
    assert "Native Plan Mode is active" in result.prompt
    assert plan_mode.PlanModeManager("session-no-plan-skill").active


def test_bare_plan_reports_status_when_already_active(isolated_plan_mode, monkeypatch):
    monkeypatch.setattr(plan_mode, "build_plan_prompt", lambda request, **_: "prompt")
    plan_mode.handle_plan_command("session-3", "first request")

    result = plan_mode.handle_plan_command("session-3")
    assert result.action == "status"
    assert result.prompt is None


def test_control_verbs_reject_extra_arguments(isolated_plan_mode):
    result = plan_mode.handle_plan_command("session-4", "approve now")
    assert result.action == "help"
    assert not plan_mode.PlanModeManager("session-4").active


def test_guard_allows_only_audited_read_only_tools(isolated_plan_mode):
    plan_mode.PlanModeManager("session-guard").activate("inspect first")

    assert plan_mode.evaluate_plan_tool_call(
        "session-guard", "read_file", {"path": "README.md"}
    ).allowed
    assert plan_mode.evaluate_plan_tool_call(
        "session-guard", "clarify", {"question": "Choose?"}
    ).allowed

    unknown = plan_mode.evaluate_plan_tool_call(
        "session-guard", "custom_plugin_tool", {}
    )
    assert not unknown.allowed
    assert unknown.code == "tool_not_read_only"

    mutation = plan_mode.evaluate_plan_tool_call(
        "session-guard", "delegate_task", {"task": "edit"}
    )
    assert not mutation.allowed


def test_plan_guard_requires_structured_batches_for_selectable_clarifications(
    isolated_plan_mode,
):
    plan_mode.PlanModeManager("session-structured-clarify").activate("plan launch")

    legacy = plan_mode.evaluate_plan_tool_call(
        "session-structured-clarify",
        "clarify",
        {
            "question": "Which launch scope?",
            "choices": ["Full store", "Catalog"],
        },
    )
    structured = plan_mode.evaluate_plan_tool_call(
        "session-structured-clarify",
        "clarify",
        {
            "questions": [
                {
                    "id": "launch_scope",
                    "header": "Scope",
                    "question": "Which launch scope?",
                    "options": [
                        {
                            "label": "Full store (Recommended)",
                            "description": "Launch transactional checkout.",
                        },
                        {
                            "label": "Catalog",
                            "description": "Defer checkout.",
                        },
                    ],
                }
            ]
        },
    )
    open_ended = plan_mode.evaluate_plan_tool_call(
        "session-structured-clarify",
        "clarify",
        {"question": "What legal constraint is not represented by fixed choices?"},
    )

    assert not legacy.allowed
    assert legacy.code == "plan_structured_clarify_required"
    assert "questions" in legacy.message
    assert structured.allowed
    assert open_ended.allowed


def test_guard_normalizes_safe_git_and_rejects_shell_composition(isolated_plan_mode):
    plan_mode.PlanModeManager("session-git").activate("inspect git")

    safe = plan_mode.evaluate_plan_tool_call(
        "session-git",
        "terminal",
        {"command": "git diff --cached --stat", "background": False},
    )
    assert safe.allowed
    assert safe.code == "audited_terminal"
    assert "--no-pager" in safe.args["command"]
    assert "--no-textconv" in safe.args["command"]
    assert "core.fsmonitor=false" in safe.args["command"]
    assert "/usr/bin/git" in safe.args["command"]
    assert "GIT_NO_LAZY_FETCH=1" in safe.args["command"]
    assert "gpg.program=/bin/false" in safe.args["command"]
    assert safe.args["pty"] is False

    for command in (
        "git status && touch bad",
        "git status --short",
        "git checkout main",
        "git diff --output=/tmp/x",
        "git diff --stat",
        "git ls-files -m",
        "git ls-files --modified",
    ):
        blocked = plan_mode.evaluate_plan_tool_call(
            "session-git", "terminal", {"command": command}
        )
        assert not blocked.allowed, command
        assert blocked.code == "terminal_not_read_only"


def test_guard_limits_plan_file_writes_to_workspace(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-files")
    manager.activate("write plan")

    premature = plan_mode.evaluate_plan_tool_call(
        "session-files",
        "write_file",
        {"path": ".hermes/plans/change.md", "content": "# Plan"},
        task_id="session-files",
    )
    assert not premature.allowed
    assert premature.code == "plan_clarification_required"

    clarified = manager.mark_clarified()
    assert clarified.clarification_count == 1

    allowed = plan_mode.evaluate_plan_tool_call(
        "session-files",
        "write_file",
        {"path": ".hermes/plans/change.md", "content": "# Plan"},
        task_id="session-files",
    )
    assert allowed.allowed

    outside = plan_mode.evaluate_plan_tool_call(
        "session-files",
        "write_file",
        {"path": "src/change.py", "content": "bad"},
        task_id="session-files",
    )
    assert not outside.allowed
    assert outside.code == "write_outside_plan_dir"

    traversal = plan_mode.evaluate_plan_tool_call(
        "session-files",
        "patch",
        {"path": ".hermes/plans/../../escape.py", "mode": "replace"},
        task_id="session-files",
    )
    assert not traversal.allowed


def test_explicit_no_questions_request_can_save_plan_without_clarify(
    isolated_plan_mode,
):
    plan_mode.PlanModeManager("session-no-questions").activate(
        "Use the documented defaults and do not ask questions"
    )

    decision = plan_mode.evaluate_plan_tool_call(
        "session-no-questions",
        "write_file",
        {"path": ".hermes/plans/explicit.md", "content": "# Plan"},
        task_id="session-no-questions",
    )

    assert decision.allowed
    assert decision.code == "plan_file_write"


def test_new_plan_request_resets_clarification_checkpoint(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-reset-clarify")
    manager.activate("first plan")
    manager.mark_clarified()
    assert manager.state.clarification_count == 1

    manager.activate("second plan")

    assert manager.state.clarification_count == 0


def test_successful_clarify_result_marks_native_plan_state(isolated_plan_mode):
    from agent.tool_executor import _record_native_plan_clarification

    class Agent:
        session_id = "session-clarify-result"

    manager = plan_mode.PlanModeManager(Agent.session_id)
    manager.activate("ask before saving")

    assert _record_native_plan_clarification(
        Agent(),
        '{"answers":{"launch_scope":{"answers":["Etapas"]}}}',
    )
    assert manager.state.clarification_count == 1

    assert not _record_native_plan_clarification(
        Agent(),
        '{"error":"Question was dismissed"}',
    )
    assert manager.state.clarification_count == 1


def test_plan_completion_cannot_stop_at_post_clarify_acknowledgement(
    isolated_plan_mode,
):
    manager = plan_mode.PlanModeManager("session-plan-completion")
    manager.activate("plan the launch")
    manager.mark_clarified()

    nudge = plan_mode.build_plan_completion_nudge(
        manager.session_id,
        "Scope confirmed: complete store.",
    )

    assert nudge is not None
    assert "do not stop at an acknowledgement" in nudge.lower()
    assert "save" in nudge.lower()


def test_plan_completion_requires_saved_artifact_and_exact_block(
    isolated_plan_mode,
):
    manager = plan_mode.PlanModeManager("session-plan-artifact")
    manager.activate("plan the launch")
    manager.mark_clarified()

    plan_body = """# Launch plan

## Summary

Ship the validated launch safely.

Locked decisions:

- Use the existing application stack.

Out of scope for v1: billing migrations and unrelated redesigns.

## Implementation plan

1. Add the guarded launch path.

## Tests and acceptance criteria

- The production smoke test passes.

## Alocação de modelos

| Trabalho | Modelo | Esforço | Finalidade | Motivo de eficiência | Gatilho de escalada |
|---|---|---|---|---|---|
| Executar o plano | gpt-5.6-sol | xhigh | Implementação e validação | Um agente mantém o contexto | Escalar se houver bloqueio externo |
"""
    manager.mark_plan_artifact_saved(".hermes/plans/launch.md", plan_body)

    assert manager.state.plan_artifact_count == 1
    assert manager.state.plan_artifact_path == ".hermes/plans/launch.md"
    assert (
        plan_mode.build_plan_completion_nudge(
            manager.session_id,
            "The plan is ready.",
        )
        is not None
    )
    assert (
        plan_mode.build_plan_completion_nudge(
            manager.session_id,
            f"<proposed_plan>\n{plan_body}\n</proposed_plan>",
        )
        is None
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            """# Launch plan

Locked decisions:

- Keep the current stack.

Out of scope for v1: unrelated work.

## Implementation plan

1. Ship it.

## Tests and acceptance criteria

- Smoke test passes.
""",
            "summary",
        ),
        (
            """# Launch plan

## Summary

Ship it safely.

Out of scope for v1: unrelated work.

## Implementation plan

1. Ship it.

## Tests and acceptance criteria

- Smoke test passes.
""",
            "locked decisions",
        ),
        (
            """# Launch plan

## Summary

Ship it safely.

Locked decisions:

- Keep the current stack.

## Implementation plan

1. Ship it.

## Tests and acceptance criteria

- Smoke test passes.
""",
            "out-of-scope",
        ),
        (
            """# Launch plan

## Summary

Ship it safely.

Locked decisions:

- Keep the current stack.

Out of scope for v1: unrelated work.

## Tests and acceptance criteria

- Smoke test passes.
""",
            "implementation",
        ),
        (
            """# Launch plan

## Summary

Ship it safely.

Locked decisions:

- Keep the current stack.

Out of scope for v1: unrelated work.

## Implementation plan

1. Ship it.
""",
            "acceptance",
        ),
        (
            """# Launch plan

## Summary

Ship it safely.

Locked decisions:

- Keep the current stack.

Out of scope for v1: unrelated work.

## Implementation plan

1. Ship it.

## Tests and acceptance criteria

- Smoke test passes.
""",
            "alocação de modelos",
        ),
    ],
)
def test_plan_completion_enforces_codex_executive_summary_contract(
    isolated_plan_mode,
    body,
    expected,
):
    manager = plan_mode.PlanModeManager(f"session-missing-{expected}")
    manager.activate("plan the launch")
    manager.mark_clarified()
    manager.mark_plan_artifact_saved(".hermes/plans/launch.md", body)

    nudge = plan_mode.build_plan_completion_nudge(
        manager.session_id,
        f"<proposed_plan>\n{body}\n</proposed_plan>",
    )

    assert nudge is not None
    assert expected in nudge.lower()


def test_plan_completion_requires_the_rendered_plan_to_match_saved_artifact(
    isolated_plan_mode,
):
    manager = plan_mode.PlanModeManager("session-plan-mismatch")
    manager.activate("plan the launch")
    manager.mark_clarified()
    saved = """# Launch plan

## Summary

Ship version A.

Locked decisions:

- Keep the current stack.

Out of scope for v1: unrelated work.

## Implementation plan

1. Ship A.

## Tests and acceptance criteria

- Smoke test passes.
"""
    rendered = saved.replace("Ship version A.", "Ship version B.")
    manager.mark_plan_artifact_saved(".hermes/plans/launch.md", saved)

    nudge = plan_mode.build_plan_completion_nudge(
        manager.session_id,
        f"<proposed_plan>\n{rendered}\n</proposed_plan>",
    )

    assert nudge is not None
    assert "match the saved plan" in nudge.lower()


def test_plan_completion_does_not_swallow_the_required_question(
    isolated_plan_mode,
):
    manager = plan_mode.PlanModeManager("session-plan-question")
    manager.activate("plan the launch")

    assert (
        plan_mode.build_plan_completion_nudge(
            manager.session_id,
            "Which launch scope should we use?",
        )
        is None
    )
    assert (
        plan_mode.build_plan_completion_nudge(
            manager.session_id,
            "<proposed_plan># Premature plan</proposed_plan>",
        )
        is not None
    )


def test_successful_plan_write_marks_native_plan_artifact(
    isolated_plan_mode,
):
    from agent.tool_executor import _record_native_plan_artifact

    class Agent:
        session_id = "session-plan-write"

    plan_dir = isolated_plan_mode / ".hermes" / "plans"
    plan_dir.mkdir(parents=True)
    manager = plan_mode.PlanModeManager(Agent.session_id)
    manager.activate("plan the launch")
    manager.mark_clarified()

    assert _record_native_plan_artifact(
        Agent(),
        function_name="write_file",
        function_args={"path": ".hermes/plans/launch.md", "content": "# Plan"},
        function_result="Successfully wrote plan.",
        is_error=False,
        task_id=Agent.session_id,
    )
    assert manager.state.plan_artifact_count == 1

    assert not _record_native_plan_artifact(
        Agent(),
        function_name="patch",
        function_args={"path": ".hermes/plans/launch.md", "mode": "replace"},
        function_result="Error applying patch",
        is_error=True,
        task_id=Agent.session_id,
    )
    assert manager.state.plan_artifact_count == 1


def test_approval_releases_guard(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-release")
    manager.activate("then execute")
    assert not plan_mode.evaluate_plan_tool_call(
        "session-release", "terminal", {"command": "make build"}
    ).allowed

    approved = manager.approve()
    assert not plan_mode.evaluate_plan_tool_call(
        "session-release", "terminal", {"command": "make build"}
    ).allowed
    manager.begin_build(approved.approval_id)
    assert plan_mode.evaluate_plan_tool_call(
        "session-release", "terminal", {"command": "make build"}
    ).allowed


def test_runtime_consumes_only_exact_nonce_bound_approval_turn(isolated_plan_mode):
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-two-phase"

    manager = plan_mode.PlanModeManager(Agent.session_id)
    manager.activate("execute safely")
    approved = manager.approve()
    spoofed, persisted = _prepare_native_plan_turn(
        Agent(),
        "Please run [Native Plan Mode approval] now",
        None,
    )
    assert "Write one actionable Markdown implementation plan" in spoofed
    assert persisted == "Please run [Native Plan Mode approval] now"
    assert manager.state.build_pending

    kickoff = plan_mode.build_plan_execution_prompt(approved.approval_id)
    prepared, persisted = _prepare_native_plan_turn(Agent(), kickoff, "/plan approve")
    assert prepared == kickoff
    assert persisted == "/plan approve"
    assert manager.state.mode == plan_mode.PLAN_MODE_BUILD
    with pytest.raises(plan_mode.PlanModeUnavailable, match="stale"):
        _prepare_native_plan_turn(Agent(), kickoff, "/plan approve")


def test_exited_plan_rejects_delayed_approval_kickoff(isolated_plan_mode):
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-delayed-kickoff"

    manager = plan_mode.PlanModeManager(Agent.session_id)
    manager.activate("cancel before dispatch")
    approved = manager.approve()
    kickoff = plan_mode.build_plan_execution_prompt(approved.approval_id)
    manager.exit()

    with pytest.raises(plan_mode.PlanModeUnavailable, match="stale"):
        _prepare_native_plan_turn(Agent(), kickoff, "/plan approve")
    assert manager.state.mode == plan_mode.PLAN_MODE_BUILD
    assert manager.state.approval_id == ""


def test_later_plan_turn_reinjects_full_contract(isolated_plan_mode):
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-reminder"

    plan_mode.PlanModeManager(Agent.session_id).activate("design safely")
    prepared, persisted = _prepare_native_plan_turn(Agent(), "Prefer option B", None)
    assert "Explore first, ask second" in prepared
    assert "clarify(questions=[...])" in prepared
    assert "<proposed_plan>" in prepared
    assert "Prefer option B" in prepared
    assert persisted == "Prefer option B"


def test_inactive_plan_preserves_multimodal_user_message(isolated_plan_mode):
    """Normal image turns must not be flattened by the Plan Mode prologue."""
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-normal-multimodal"

    original = [
        {"type": "text", "text": "Read this screenshot"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,VALIDIMAGE"},
        },
    ]

    prepared, persisted = _prepare_native_plan_turn(Agent(), original, None)

    assert prepared is original
    assert persisted is None


def test_later_plan_turn_preserves_multimodal_parts(isolated_plan_mode):
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-multimodal-reminder"

    data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    original = [
        {"type": "text", "text": "Use this screenshot"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    plan_mode.PlanModeManager(Agent.session_id).activate("audit the screenshot")

    prepared, persisted = _prepare_native_plan_turn(Agent(), original, None)

    assert isinstance(prepared, list)
    assert prepared[0]["type"] == "text"
    assert "Use this screenshot" in prepared[0]["text"]
    assert "Explore first, ask second" in prepared[0]["text"]
    assert prepared[1] == original[1]
    assert data_url not in prepared[0]["text"]
    assert persisted is original


def test_repeated_approval_is_idempotent_while_kickoff_pending(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-approval-retry")
    manager.activate("retry safely")
    first = manager.approve()
    second = manager.approve()
    assert second.approval_id == first.approval_id
    assert second.build_pending


def test_approved_build_grant_is_scoped_to_the_exact_execution_turn(
    isolated_plan_mode,
):
    manager = plan_mode.PlanModeManager("session-approved-build")
    manager.activate("build safely")
    approval = manager.approve()

    started = manager.begin_build(approval.approval_id)
    assert started.approved_build

    completed = manager.complete_build(approval.approval_id)
    assert completed.mode == plan_mode.PLAN_MODE_BUILD
    assert not completed.approved_build
    assert completed.approval_id == ""
    assert completed.last_action == "build_completed"


def test_interrupted_approved_build_continuation_reinjects_plan_constraints(
    isolated_plan_mode,
):
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-approved-build-continuation"

    manager = plan_mode.PlanModeManager(Agent.session_id)
    manager.activate("build safely")
    approval = manager.approve()
    manager.begin_build(approval.approval_id)

    prepared, persisted = _prepare_native_plan_turn(
        Agent(),
        "Continue, but use Open Design.",
        None,
    )

    assert "approved plan execution continuation" in prepared.lower()
    assert "reference-only historical code" in prepared.lower()
    assert "Continue, but use Open Design." in prepared
    assert persisted == "Continue, but use Open Design."
    assert manager.state.approved_build


def test_approved_plan_constraint_blocks_reference_only_cherry_pick(
    isolated_plan_mode,
):
    workspace = isolated_plan_mode
    artifact = workspace / ".hermes" / "plans" / "plan.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# Plan\n\n"
        "The historical connector is reference-only, not authorization for "
        "merge or whole cherry-pick.\n"
    )
    artifact.write_text(body, encoding="utf-8")

    manager = plan_mode.PlanModeManager("session-no-whole-cherry-pick")
    manager.activate("implement selectively")
    manager.mark_plan_artifact_saved(str(artifact), body)
    approval = manager.approve()
    manager.begin_build(approval.approval_id)

    blocked = plan_mode.evaluate_plan_tool_call(
        "session-no-whole-cherry-pick",
        "terminal",
        {"command": "set -euo pipefail\ngit cherry-pick abc123 def456"},
    )
    inspection = plan_mode.evaluate_plan_tool_call(
        "session-no-whole-cherry-pick",
        "terminal",
        {"command": "git show abc123 -- src/integration.py"},
    )

    assert not blocked.allowed
    assert blocked.code == "approved_plan_reference_only_history"
    assert "git show" in blocked.message
    assert inspection.allowed


def test_reference_only_guard_allows_current_plan_commits(
    isolated_plan_mode,
):
    workspace = isolated_plan_mode
    artifact = workspace / ".hermes" / "plans" / "plan.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    forbidden_sha = "4536200bfc682ac1a2a5e2fa566674fd91ed3b86"
    body = (
        "# Plan\n\n"
        f"The historical connector at `{forbidden_sha}` is reference-only, "
        "not authorization for merge or whole cherry-pick.\n\n"
        "## Plan Integrity\n\n"
        "Historical code is reference, not authorization for whole cherry-pick.\n"
    )
    artifact.write_text(body, encoding="utf-8")

    manager = plan_mode.PlanModeManager("session-scoped-history-guard")
    manager.activate("implement selectively")
    manager.mark_plan_artifact_saved(str(artifact), body)
    approval = manager.approve()
    manager.begin_build(approval.approval_id)

    own_commit = plan_mode.evaluate_plan_tool_call(
        "session-scoped-history-guard",
        "terminal",
        {"command": "git cherry-pick ab469d6"},
    )
    own_branch = plan_mode.evaluate_plan_tool_call(
        "session-scoped-history-guard",
        "terminal",
        {"command": "git merge --ff-only feat/shipping-20260829"},
    )
    historical_commit = plan_mode.evaluate_plan_tool_call(
        "session-scoped-history-guard",
        "terminal",
        {"command": "git cherry-pick 4536200b"},
    )

    assert own_commit.allowed
    assert own_branch.allowed
    assert not historical_commit.allowed
    assert historical_commit.code == "approved_plan_reference_only_history"


def test_plan_exit_revokes_interrupted_approved_build_grant(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-exit-approved-build")
    manager.activate("build safely")
    approval = manager.approve()
    manager.begin_build(approval.approval_id)

    exited = manager.exit()

    assert exited.mode == plan_mode.PLAN_MODE_BUILD
    assert exited.last_action == "exited"
    assert exited.approval_id == ""
    assert not exited.approved_build


def test_concurrent_approvals_converge_on_one_nonce(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-approval-race")
    manager.activate("race safely")
    barrier = threading.Barrier(2)
    approval_ids = []
    failures = []

    def approve():
        try:
            barrier.wait(timeout=2)
            approval_ids.append(manager.approve().approval_id)
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert failures == []
    assert len(approval_ids) == 2
    assert len(set(approval_ids)) == 1


def test_concurrent_exact_kickoff_admits_execution_only_once(isolated_plan_mode):
    from agent.conversation_loop import _prepare_native_plan_turn

    class Agent:
        session_id = "session-kickoff-race"

    manager = plan_mode.PlanModeManager(Agent.session_id)
    manager.activate("execute once")
    approved = manager.approve()
    kickoff = plan_mode.build_plan_execution_prompt(approved.approval_id)
    barrier = threading.Barrier(2)
    successes = []
    failures = []

    def prepare():
        try:
            barrier.wait(timeout=2)
            successes.append(
                _prepare_native_plan_turn(Agent(), kickoff, "/plan approve")
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=prepare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert successes == [(kickoff, "/plan approve")]
    assert len(failures) == 1
    assert isinstance(failures[0], plan_mode.PlanModeUnavailable)
    assert manager.state.mode == plan_mode.PLAN_MODE_BUILD


def test_pending_approval_uses_rollback_safe_plan_wire_value(isolated_plan_mode):
    pending = plan_mode.PlanModeManager("session-wire-safe")
    pending.activate("safe rollback")
    state = pending.approve()
    payload = state.to_json()
    assert '"mode": "plan"' in payload
    assert "build_pending" not in payload
    with pytest.raises(ValueError):
        plan_mode.PlanModeState.from_json(
            '{"schema_version": 1, "mode": "build_pending"}'
        )


def test_agent_executor_dispatch_guard_uses_session_state(isolated_plan_mode):
    from agent.tool_executor import _apply_native_plan_guard

    class Agent:
        session_id = "session-dispatch"

    plan_mode.PlanModeManager(Agent.session_id).activate("guard dispatch")
    args, block, code = _apply_native_plan_guard(
        Agent(),
        function_name="terminal",
        function_args={"command": "touch forbidden"},
        effective_task_id=Agent.session_id,
    )
    assert args == {"command": "touch forbidden"}
    assert block and "Blocked by native Plan Mode" in block
    assert code == "terminal_not_read_only"


def test_plan_state_migrates_across_session_rotation(isolated_plan_mode):
    plan_mode.PlanModeManager("old-session").activate("long plan")

    assert plan_mode.migrate_plan_mode_to_session(
        "old-session", "new-session", reason="compression"
    )
    assert plan_mode.PlanModeManager("new-session").active
    assert plan_mode.PlanModeManager("new-session").state.request == "long plan"
    assert not plan_mode.PlanModeManager("old-session").active


def test_child_session_inherits_active_parent_boundary_fail_closed(isolated_plan_mode):
    db = plan_mode._get_session_db()
    db.create_session(session_id="parent-session", source="cli")
    db.create_session(
        session_id="child-session",
        source="cli",
        parent_session_id="parent-session",
    )
    plan_mode.PlanModeManager("parent-session").activate("safe lineage")

    inherited = plan_mode.PlanModeManager("child-session").state
    assert inherited.active
    assert inherited.request == "safe lineage"
    assert inherited.last_action == "inherited"


def test_state_read_failure_keeps_mutations_blocked(isolated_plan_mode, monkeypatch):
    class BrokenDB:
        def get_meta(self, key):
            raise OSError("database unavailable")

    monkeypatch.setattr(plan_mode, "_get_session_db", lambda: BrokenDB())

    with pytest.raises(plan_mode.PlanModeStateUnavailable):
        plan_mode.load_plan_mode("session-db-error")
    read = plan_mode.evaluate_plan_tool_call(
        "session-db-error", "read_file", {"path": "README.md"}
    )
    mutation = plan_mode.evaluate_plan_tool_call(
        "session-db-error", "terminal", {"command": "touch forbidden"}
    )
    plan_write = plan_mode.evaluate_plan_tool_call(
        "session-db-error",
        "write_file",
        {"path": ".hermes/plans/unsafe.md", "content": "unsafe"},
        task_id="session-db-error",
    )
    assert read.allowed
    assert not mutation.allowed
    assert not plan_write.allowed


def test_corrupt_persisted_state_fails_closed(isolated_plan_mode):
    db = plan_mode._get_session_db()
    db.set_meta("plan:session-corrupt", "{not-json")

    with pytest.raises(plan_mode.PlanModeStateUnavailable):
        plan_mode.load_plan_mode("session-corrupt")
    decision = plan_mode.evaluate_plan_tool_call(
        "session-corrupt", "delegate_task", {"task": "mutate"}
    )
    assert not decision.allowed


def test_executor_guard_unexpected_error_is_never_fail_open(
    isolated_plan_mode, monkeypatch
):
    from agent.tool_executor import _apply_native_plan_guard

    class Agent:
        session_id = "session-guard-crash"

    monkeypatch.setattr(
        plan_mode,
        "evaluate_plan_tool_call",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("policy crash")),
    )
    _args, block, code = _apply_native_plan_guard(
        Agent(),
        function_name="terminal",
        function_args={"command": "touch forbidden"},
        effective_task_id=Agent.session_id,
    )
    assert block and "safety policy could not be evaluated" in block
    assert code == "plan_guard_error"


def test_successful_state_write_is_not_undone_by_failed_readback(monkeypatch):
    writes = {}

    class CommitThenUnreadableDB:
        def set_meta(self, key, value):
            writes[key] = value

        def get_meta(self, key):
            raise OSError("readback unavailable")

    monkeypatch.setattr(plan_mode, "_get_session_db", lambda: CommitThenUnreadableDB())
    state = plan_mode.PlanModeState(mode=plan_mode.PLAN_MODE_PLAN, request="persist")
    assert plan_mode.save_plan_mode("session-write", state)
    assert writes["plan:session-write"] == state.to_json()


def test_audited_git_diff_never_executes_textconv(isolated_plan_mode):
    workspace = isolated_plan_mode
    marker = workspace / "textconv-executed"
    driver = workspace / "evil-textconv.sh"
    driver.write_text(f'#!/bin/sh\ntouch {marker}\ncat "$1"\n', encoding="utf-8")
    driver.chmod(0o755)
    (workspace / ".gitattributes").write_text("*.bin diff=evil\n", encoding="utf-8")
    target = workspace / "sample.bin"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Plan Test"], cwd=workspace, check=True
    )
    subprocess.run(
        ["git", "config", "diff.evil.textconv", str(driver)], cwd=workspace, check=True
    )
    subprocess.run(
        ["git", "add", ".gitattributes", "sample.bin"], cwd=workspace, check=True
    )
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=workspace, check=True)
    target.write_text("after\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "add", "sample.bin"], cwd=workspace, check=True)
    marker.unlink(missing_ok=True)

    plan_mode.PlanModeManager("session-textconv").activate("inspect safely")
    decision = plan_mode.evaluate_plan_tool_call(
        "session-textconv",
        "terminal",
        {"command": "git diff --cached -- sample.bin", "workdir": str(workspace)},
        task_id="session-textconv",
    )
    assert decision.allowed
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(decision.args["command"], cwd=workspace, check=True, shell=True)
    status_after = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert not marker.exists()
    assert target.read_text(encoding="utf-8") == "after\n"
    assert status_after == status_before


def test_audited_git_diff_never_executes_clean_filter(isolated_plan_mode):
    workspace = isolated_plan_mode
    marker = workspace / "clean-filter-executed"
    driver = workspace / "evil-clean.sh"
    driver.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    driver.chmod(0o755)
    (workspace / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    target = workspace / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["/usr/bin/git", "config", "user.email", "test@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "config", "user.name", "Plan Test"], cwd=workspace, check=True
    )
    subprocess.run(
        ["/usr/bin/git", "config", "filter.evil.clean", str(driver)],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "add", ".gitattributes", "sample.txt"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "commit", "-qm", "fixture"], cwd=workspace, check=True
    )
    marker.unlink(missing_ok=True)
    target.write_text("after\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "add", "sample.txt"], cwd=workspace, check=True)
    marker.unlink(missing_ok=True)

    plan_mode.PlanModeManager("session-clean-filter").activate("inspect safely")
    status = plan_mode.evaluate_plan_tool_call(
        "session-clean-filter",
        "terminal",
        {"command": "git status --short", "workdir": str(workspace)},
        task_id="session-clean-filter",
    )
    assert not status.allowed
    assert not marker.exists()
    decision = plan_mode.evaluate_plan_tool_call(
        "session-clean-filter",
        "terminal",
        {"command": "git diff --cached -- sample.txt", "workdir": str(workspace)},
        task_id="session-clean-filter",
    )
    assert decision.allowed
    subprocess.run(decision.args["command"], cwd=workspace, check=True, shell=True)
    assert not marker.exists()
    worktree_diff = plan_mode.evaluate_plan_tool_call(
        "session-clean-filter",
        "terminal",
        {"command": "git diff -- sample.txt", "workdir": str(workspace)},
        task_id="session-clean-filter",
    )
    assert not worktree_diff.allowed


def test_audited_terminal_ignores_hostile_path_wrappers(
    isolated_plan_mode, monkeypatch
):
    workspace = isolated_plan_mode
    marker = workspace / "hostile-git-ran"
    hostile_bin = workspace / "hostile-bin"
    hostile_bin.mkdir()
    wrapper = hostile_bin / "git"
    wrapper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hostile_bin}:{os.environ.get('PATH', '')}")
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=workspace, check=True)

    plan_mode.PlanModeManager("session-hostile-path").activate("inspect safely")
    decision = plan_mode.evaluate_plan_tool_call(
        "session-hostile-path",
        "terminal",
        {"command": "git rev-parse --is-inside-work-tree", "workdir": str(workspace)},
        task_id="session-hostile-path",
    )
    assert decision.allowed
    subprocess.run(decision.args["command"], cwd=workspace, check=True, shell=True)
    assert not marker.exists()


def test_audited_git_ignores_inherited_repository_redirects(isolated_plan_mode):
    workspace = isolated_plan_mode
    external = workspace.parent / "external-repo"
    external.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=external, check=True)

    plan_mode.PlanModeManager("session-hostile-git-env").activate("inspect safely")
    decision = plan_mode.evaluate_plan_tool_call(
        "session-hostile-git-env",
        "terminal",
        {"command": "git rev-parse --show-toplevel", "workdir": str(workspace)},
        task_id="session-hostile-git-env",
    )
    assert decision.allowed
    command = (
        f"export GIT_DIR={shlex.quote(str(external / '.git'))} "
        f"GIT_WORK_TREE={shlex.quote(str(external))}; {decision.args['command']}"
    )
    completed = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert Path(completed.stdout.strip()).resolve() == workspace.resolve()


def test_audited_terminal_clears_inherited_dynamic_loader_hooks(isolated_plan_mode):
    workspace = isolated_plan_mode
    marker = workspace / "preload-executed"
    source = workspace / "evil-preload.c"
    library = workspace / "evil-preload.so"
    source.write_text(
        "#include <fcntl.h>\n"
        "#include <unistd.h>\n"
        "__attribute__((constructor)) static void run(void) {\n"
        f'  int fd = open("{marker}", O_WRONLY | O_CREAT, 0600);\n'
        "  if (fd >= 0) close(fd);\n"
        "}\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["/usr/bin/gcc", "-shared", "-fPIC", "-o", str(library), str(source)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        pytest.skip("gcc is required for the dynamic-loader adversarial test")
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=workspace, check=True)

    plan_mode.PlanModeManager("session-hostile-loader").activate("inspect safely")
    decision = plan_mode.evaluate_plan_tool_call(
        "session-hostile-loader",
        "terminal",
        {"command": "git rev-parse --is-inside-work-tree", "workdir": str(workspace)},
        task_id="session-hostile-loader",
    )
    assert decision.allowed
    command = (
        f"export LD_PRELOAD={shlex.quote(str(library))}; {decision.args['command']}"
    )
    completed = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "true"
    assert not marker.exists()


@pytest.mark.parametrize(
    "command",
    (
        "git log --show-signature",
        "git show --pretty=fuller HEAD",
        "git log --format=%G? -1",
    ),
)
def test_audited_git_rejects_signature_execution_surfaces(isolated_plan_mode, command):
    plan_mode.PlanModeManager("session-gpg").activate("inspect safely")
    decision = plan_mode.evaluate_plan_tool_call(
        "session-gpg", "terminal", {"command": command}
    )
    assert not decision.allowed
