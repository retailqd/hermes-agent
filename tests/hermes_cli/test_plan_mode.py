from __future__ import annotations

from pathlib import Path

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
    assert approved.mode == plan_mode.PLAN_MODE_BUILD
    assert approved.last_action == "approved"
    assert not plan_mode.PlanModeManager("session-1").active


def test_plan_command_requires_explicit_approval(isolated_plan_mode, monkeypatch):
    monkeypatch.setattr(plan_mode, "build_plan_prompt", lambda request, **_: f"PLAN:{request}")

    entered = plan_mode.handle_plan_command("session-2", "add native mode")
    assert entered.action == "enter"
    assert entered.plan_mode == "plan"
    assert entered.prompt == "PLAN:add native mode"

    status = plan_mode.handle_plan_command("session-2", "status")
    assert status.action == "status"
    assert "Mutating tools are blocked" in status.message

    approved = plan_mode.handle_plan_command("session-2", "approve")
    assert approved.action == "approve"
    assert approved.plan_mode == "build"
    assert approved.prompt == plan_mode.PLAN_EXECUTION_PROMPT


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
    assert "user has invoked" not in prompt.lower()
    assert "skill content" not in prompt.lower()


def test_plan_command_enters_without_installed_plan_skill(isolated_plan_mode, monkeypatch):
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

    assert plan_mode.evaluate_plan_tool_call("session-guard", "read_file", {"path": "README.md"}).allowed
    assert plan_mode.evaluate_plan_tool_call("session-guard", "clarify", {"question": "Choose?"}).allowed

    unknown = plan_mode.evaluate_plan_tool_call("session-guard", "custom_plugin_tool", {})
    assert not unknown.allowed
    assert unknown.code == "tool_not_read_only"

    mutation = plan_mode.evaluate_plan_tool_call("session-guard", "delegate_task", {"task": "edit"})
    assert not mutation.allowed


def test_guard_normalizes_safe_git_and_rejects_shell_composition(isolated_plan_mode):
    plan_mode.PlanModeManager("session-git").activate("inspect git")

    safe = plan_mode.evaluate_plan_tool_call(
        "session-git",
        "terminal",
        {"command": "git diff --stat", "background": False},
    )
    assert safe.allowed
    assert safe.code == "audited_terminal"
    assert "--no-pager" in safe.args["command"]
    assert safe.args["pty"] is False

    for command in ("git status && touch bad", "git checkout main", "git diff --output=/tmp/x"):
        blocked = plan_mode.evaluate_plan_tool_call("session-git", "terminal", {"command": command})
        assert not blocked.allowed, command
        assert blocked.code == "terminal_not_read_only"


def test_guard_limits_plan_file_writes_to_workspace(isolated_plan_mode):
    plan_mode.PlanModeManager("session-files").activate("write plan")

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


def test_approval_releases_guard(isolated_plan_mode):
    manager = plan_mode.PlanModeManager("session-release")
    manager.activate("then execute")
    assert not plan_mode.evaluate_plan_tool_call("session-release", "terminal", {"command": "make build"}).allowed

    manager.approve()
    assert plan_mode.evaluate_plan_tool_call("session-release", "terminal", {"command": "make build"}).allowed


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

    assert plan_mode.migrate_plan_mode_to_session("old-session", "new-session", reason="compression")
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
