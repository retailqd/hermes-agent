import sys
from types import ModuleType, SimpleNamespace

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


class FakeAgent:
    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self.steers = []
        self.runs = []

    def steer(self, text):
        self.steers.append(text)
        return True

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        final = f"ran: {user_message}"
        messages.append({"role": "assistant", "content": final})
        return {"final_response": final, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []

    async def session_update(self, *args, **kwargs):
        if kwargs:
            self.updates.append((kwargs.get("session_id"), kwargs.get("update")))
        else:
            self.updates.append((args[0], args[1]))

    async def request_permission(self, *args, **kwargs):
        return SimpleNamespace(outcome="allow")


class NoopDb:
    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None


def make_agent_and_state():
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    conn = CaptureConn()
    acp_agent.on_connect(conn)
    return acp_agent, state, fake, conn


def test_acp_real_agent_gets_session_db_for_recall(monkeypatch):
    """ACP sessions persist to SessionDB; recall must receive the same DB handle."""
    captured = {}
    sentinel_db = NoopDb()

    class CapturingAgent(FakeAgent):
        def __init__(self, **kwargs):
            super().__init__()
            captured.update(kwargs)

    def mod(name, **attrs):
        module = ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    monkeypatch.setitem(sys.modules, "run_agent", mod("run_agent", AIAgent=CapturingAgent))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        mod("hermes_cli.config", load_config=lambda: {"model": {"default": "m", "provider": "p"}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        mod(
            "hermes_cli.runtime_provider",
            resolve_runtime_provider=lambda **_kwargs: {
                "provider": "p",
                "api_mode": "chat_completions",
                "base_url": "u",
                "api_key": "k",
                "command": None,
                "args": [],
            },
        ),
    )

    manager = SessionManager(db=sentinel_db)
    agent = manager._make_agent(session_id="acp-session", cwd=".")

    assert isinstance(agent, CapturingAgent)
    assert captured["session_db"] is sentinel_db
    assert captured["platform"] == "acp"
    assert captured["session_id"] == "acp-session"


@pytest.mark.asyncio
async def test_acp_steer_slash_command_injects_into_running_agent():
    acp_agent, state, fake, _conn = make_agent_and_state()
    state.is_running = True

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/steer prefer the simpler fix")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.steers == ["prefer the simpler fix"]
    assert fake.runs == []


@pytest.mark.asyncio
async def test_acp_steer_after_zed_interrupt_replays_interrupted_prompt_with_guidance():
    acp_agent, state, fake, _conn = make_agent_and_state()
    state.interrupted_prompt_text = "write hi to a text file"

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/steer write HELLO instead")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.steers == []
    assert fake.runs == [
        "write hi to a text file\n\nUser correction/guidance after interrupt: write HELLO instead"
    ]
    assert state.interrupted_prompt_text == ""


@pytest.mark.asyncio
async def test_acp_steer_on_idle_session_runs_as_regular_prompt():
    # /steer on an idle session (no running turn, nothing to salvage) should
    # run the steer payload as a normal user prompt — NOT silently append it
    # to state.queued_prompts. Without this, users on Zed / other ACP clients
    # see their /steer turn into "queued for the next turn" when they never
    # typed /queue. Matches gateway/run.py ~L4898 idle-/steer behavior.
    acp_agent, state, fake, _conn = make_agent_and_state()

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/steer summarize the README")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.steers == []
    assert fake.runs == ["summarize the README"]
    assert state.queued_prompts == []


@pytest.mark.asyncio
async def test_acp_queue_slash_command_adds_next_turn_without_running_now():
    acp_agent, state, fake, _conn = make_agent_and_state()

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/queue run the tests after this")],
    )

    assert response.stop_reason == "end_turn"
    assert state.queued_prompts == ["run the tests after this"]
    assert fake.runs == []


@pytest.mark.asyncio
async def test_acp_prompt_drains_queued_turns_after_current_run():
    acp_agent, state, fake, conn = make_agent_and_state()
    state.queued_prompts.append("then run tests")

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="make the change")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == ["make the change", "then run tests"]
    assert state.queued_prompts == []
    agent_messages = [u for _sid, u in conn.updates if getattr(u, "session_update", None) == "agent_message_chunk"]
    assert len(agent_messages) >= 2


@pytest.mark.asyncio
async def test_acp_plan_enter_runs_expanded_cache_safe_prompt(monkeypatch):
    acp_agent, state, fake, _conn = make_agent_and_state()

    monkeypatch.setattr(
        "hermes_cli.plan_mode.handle_plan_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            action="enter",
            plan_mode="plan",
            message="PLAN activated",
            prompt="expanded native plan prompt",
        ),
    )

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/plan implement it")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == ["expanded native plan prompt"]


@pytest.mark.asyncio
async def test_acp_plan_exit_is_local_and_does_not_run_model(monkeypatch):
    acp_agent, state, fake, conn = make_agent_and_state()

    monkeypatch.setattr(
        "hermes_cli.plan_mode.handle_plan_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            action="exit",
            plan_mode="build",
            message="Plan Mode exited.",
            prompt=None,
        ),
    )

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/plan exit")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == []
    assert any("Plan Mode exited." in str(update) for _sid, update in conn.updates)


@pytest.mark.asyncio
async def test_acp_plan_approval_is_rejected_mid_turn():
    acp_agent, state, fake, conn = make_agent_and_state()
    state.is_running = True

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/plan approve")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == []
    assert any("Only /plan status" in str(update) for _sid, update in conn.updates)


@pytest.mark.asyncio
async def test_acp_completed_plan_matches_codex_plain_markdown_and_opens_review(
    monkeypatch,
):
    acp_agent, state, fake, conn = make_agent_and_state()
    plan = """# Launch plan

## Summary

Ship the launch safely.
"""

    def run_plan(**kwargs):
        fake.runs.append(kwargs["user_message"])
        final = f"<proposed_plan>\n{plan}\n</proposed_plan>"
        return {
            "final_response": final,
            "messages": [{"role": "assistant", "content": final}],
        }

    fake.run_conversation = run_plan
    captured = []

    async def reject_review(_conn, session_id, plan_text):
        captured.append((session_id, plan_text))
        return False

    monkeypatch.setattr(
        "acp_adapter.plan_review.native_plan_is_active",
        lambda _session_id: True,
    )
    monkeypatch.setattr(
        "acp_adapter.plan_review.request_plan_review",
        reject_review,
    )

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="continue the plan")],
    )

    assert response.stop_reason == "end_turn"
    assert captured == [(state.session_id, plan.strip())]
    rendered = "\n".join(str(update) for _sid, update in conn.updates)
    assert "# Launch plan" in rendered
    assert "<proposed_plan>" not in rendered


@pytest.mark.asyncio
async def test_acp_plan_review_approval_executes_through_nonce_bound_plan_command(
    monkeypatch,
):
    acp_agent, state, fake, _conn = make_agent_and_state()
    calls = 0

    def run_plan(**kwargs):
        nonlocal calls
        calls += 1
        fake.runs.append(kwargs["user_message"])
        if calls == 1:
            final = "<proposed_plan>\n# Approved plan\n</proposed_plan>"
        else:
            final = "execution complete"
        return {
            "final_response": final,
            "messages": [{"role": "assistant", "content": final}],
        }

    fake.run_conversation = run_plan

    async def approve_review(_conn, _session_id, _plan_text):
        return True

    monkeypatch.setattr(
        "acp_adapter.plan_review.native_plan_is_active",
        lambda _session_id: calls == 0,
    )
    monkeypatch.setattr(
        "acp_adapter.plan_review.request_plan_review",
        approve_review,
    )
    monkeypatch.setattr(
        "hermes_cli.plan_mode.handle_plan_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            action="approve",
            plan_mode="plan",
            message="Plan approved.",
            prompt="hidden nonce-bound execution prompt",
        ),
    )

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="continue the plan")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == ["continue the plan", "hidden nonce-bound execution prompt"]


@pytest.mark.asyncio
async def test_acp_cancelled_approved_build_keeps_grant_for_next_continuation(
    monkeypatch,
):
    acp_agent, state, fake, _conn = make_agent_and_state()
    manager = __import__(
        "hermes_cli.plan_mode", fromlist=["PlanModeManager"]
    ).PlanModeManager(state.session_id)
    manager.activate("implement the approved plan")
    calls = 0

    def run_plan(**kwargs):
        nonlocal calls
        calls += 1
        fake.runs.append(kwargs["user_message"])
        if calls == 1:
            final = "<proposed_plan>\n# Approved plan\n</proposed_plan>"
        elif calls == 2:
            pending = manager.state
            manager.begin_build(pending.approval_id)
            state.cancel_event.set()
            final = "interrupted while adding owner guidance"
        else:
            final = (
                "execution continuation complete\n"
                '<approved_plan_execution status="complete" />'
            )
        return {
            "final_response": final,
            "messages": [{"role": "assistant", "content": final}],
        }

    fake.run_conversation = run_plan

    async def approve_review(_conn, _session_id, _plan_text):
        return True

    monkeypatch.setattr(
        "acp_adapter.plan_review.native_plan_is_active",
        lambda _session_id: calls == 0,
    )
    monkeypatch.setattr(
        "acp_adapter.plan_review.request_plan_review",
        approve_review,
    )

    interrupted = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="continue the plan")],
    )

    assert interrupted.stop_reason == "cancelled"
    assert manager.state.approved_build

    resumed = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="use Open Design and continue")],
    )

    assert resumed.stop_reason == "end_turn"
    assert manager.state.last_action == "build_completed"
    assert not manager.state.approved_build


@pytest.mark.asyncio
async def test_acp_approved_build_auto_continues_until_explicit_completion():
    acp_agent, state, fake, conn = make_agent_and_state()
    manager = __import__(
        "hermes_cli.plan_mode", fromlist=["PlanModeManager"]
    ).PlanModeManager(state.session_id)
    manager.activate("implement every ordinary step in the approved plan")
    pending = manager.approve()
    manager.begin_build(pending.approval_id)
    calls = 0

    def run_plan(**kwargs):
        nonlocal calls
        calls += 1
        fake.runs.append(kwargs["user_message"])
        if calls == 1:
            final = "## Pending\n\nOrdinary implementation and tests remain."
        else:
            final = (
                "All approved ordinary work and verification are complete.\n"
                '<approved_plan_execution status="complete" />'
            )
        return {
            "final_response": final,
            "messages": [{"role": "assistant", "content": final}],
        }

    fake.run_conversation = run_plan

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="continue the approved plan")],
    )

    assert response.stop_reason == "end_turn"
    assert calls == 2
    assert manager.state.last_action == "build_completed"
    assert not manager.state.approved_build
    rendered = "\n".join(str(update) for _sid, update in conn.updates)
    assert "All approved ordinary work" in rendered
    assert "Ordinary implementation and tests remain" not in rendered
    assert "approved_plan_execution" not in rendered


@pytest.mark.asyncio
async def test_acp_approved_build_high_impact_block_preserves_grant():
    acp_agent, state, fake, conn = make_agent_and_state()
    manager = __import__(
        "hermes_cli.plan_mode", fromlist=["PlanModeManager"]
    ).PlanModeManager(state.session_id)
    manager.activate("implement the approved plan until a real owner gate")
    pending = manager.approve()
    manager.begin_build(pending.approval_id)

    def run_plan(**kwargs):
        fake.runs.append(kwargs["user_message"])
        final = (
            "Blocked only on the production database migration approval.\n"
            '<approved_plan_execution status="blocked" />'
        )
        return {
            "final_response": final,
            "messages": [{"role": "assistant", "content": final}],
        }

    fake.run_conversation = run_plan

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="continue the approved plan")],
    )

    assert response.stop_reason == "end_turn"
    assert len(fake.runs) == 1
    assert manager.state.approved_build
    rendered = "\n".join(str(update) for _sid, update in conn.updates)
    assert "production database migration" in rendered
    assert "approved_plan_execution" not in rendered
