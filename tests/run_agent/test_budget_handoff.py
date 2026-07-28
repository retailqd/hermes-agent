"""Core contracts for gateway budget handoff scaffolding."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_finalizer import finalize_turn
from run_agent import _is_ephemeral_scaffolding


def _agent_for_budget_handoff():
    agent = MagicMock()
    agent.max_iterations = 300
    agent.iteration_budget = SimpleNamespace(remaining=0, used=300, max_total=300)
    agent.quiet_mode = True
    agent._gateway_budget_autocontinue_enabled = True
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = "https://example.invalid"
    agent.session_id = "session-1"
    agent.platform = "mattermost"
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "exact"
    agent.session_cost_source = "test"
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent._turn_failed_file_mutations = {}
    agent._tool_guardrail_halt_decision = None
    agent._interrupt_message = None
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = set()
    agent._response_was_previewed = False
    agent._drain_pending_steer.return_value = None
    agent._turn_completion_explainer_enabled.return_value = False
    agent._handle_max_iterations.side_effect = _append_summary_handoff
    return agent


def _append_summary_handoff(messages, api_call_count):
    messages.append({"role": "user", "content": "summarize internal handoff"})
    messages.append({"role": "assistant", "content": "unfinished work handoff"})
    return "unfinished work handoff"


def test_budget_exit_reason_is_canonical_and_handoff_messages_are_ephemeral():
    agent = _agent_for_budget_handoff()
    messages = [{"role": "user", "content": "finish task"}]

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=300,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="finish task",
        original_user_message="finish task",
        _should_review_memory=False,
        _turn_exit_reason="loop_fell_through",
    )

    assert result["turn_exit_reason"] == "budget_exhausted"
    assert result["api_calls"] == 300
    agent._emit_status.assert_not_called()
    handoff = result["messages"][-2:]
    assert [message["role"] for message in handoff] == ["user", "assistant"]
    assert all(
        message.get("_budget_continuation_synthetic") is True
        for message in handoff
    )
    assert all(_is_ephemeral_scaffolding(message) for message in handoff)


def test_budget_handoff_flag_is_recognized_by_persistence_filter():
    assert _is_ephemeral_scaffolding(
        {
            "role": "assistant",
            "content": "internal only",
            "_budget_continuation_synthetic": True,
        }
    )
    assert not _is_ephemeral_scaffolding(
        {"role": "assistant", "content": "owner-facing result"}
    )
