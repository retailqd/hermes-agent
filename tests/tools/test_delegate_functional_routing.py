from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.tools.test_delegate import _make_mock_parent
from tools.registry import registry

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_child_agent,
    _extract_trusted_parent_context,
    delegate_task,
)


def _routing_config():
    return {
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
        "max_iterations": 50,
        "model_routing": {
            "enabled": True,
            "default_class": "specialist",
            "inherit_parent_fallback": False,
            "routes": {
                "mechanical": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "low",
                },
                "specialist": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "high",
                },
                "coordinator": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                },
                "critical": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                },
            },
        },
    }


def _resolved_credentials(config, _parent):
    return {
        "provider": config.get("provider"),
        "model": config.get("model"),
        "base_url": "https://example.invalid",
        "api_key": "test-key",
        "api_mode": "codex_responses",
        "request_overrides": {},
        "max_output_tokens": None,
        "command": None,
        "args": [],
    }


def _child_from_build(**kwargs):
    child = MagicMock()
    child.model = kwargs["model"]
    child._delegate_role = kwargs["role"]
    child._delegate_function_class = kwargs["functional_class"]
    child._delegate_function_inferred = kwargs["functional_inferred_class"]
    child._delegate_function_trusted_inferred = kwargs[
        "functional_trusted_inferred_class"
    ]
    child._delegate_function_requested = kwargs["functional_requested_class"]
    child._delegate_function_source = kwargs["functional_decision_source"]
    child._delegate_route_provider = kwargs["override_provider"]
    child._delegate_route_model = kwargs["model"]
    return child


def test_schema_exposes_function_class_but_not_provider_or_model():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_props = props["tasks"]["items"]["properties"]

    assert props["function_class"]["enum"] == [
        "mechanical",
        "specialist",
        "coordinator",
        "critical",
    ]
    assert task_props["function_class"]["enum"] == props["function_class"]["enum"]
    assert "provider" not in props
    assert "model" not in props
    assert "provider" not in task_props
    assert "model" not in task_props


def test_extract_trusted_parent_context_uses_latest_real_user_message():
    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "working"},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Approve production release gate"},
                {"type": "image_url", "image_url": "ignored"},
            ],
        },
        {"role": "assistant", "content": "tool call"},
    ]

    assert (
        _extract_trusted_parent_context(messages) == "Approve production release gate"
    )

    synthetic_latest = messages + [
        {
            "role": "user",
            "content": "[ASYNC DELEGATION BATCH COMPLETE] deploy",
        },
    ]
    assert (
        _extract_trusted_parent_context(synthetic_latest)
        == "Approve production release gate"
    )


@patch("tools.delegation_live_log.create_live_transcripts", return_value=(None, [], []))
@patch("tools.delegate_tool._run_single_child")
@patch("tools.delegate_tool._build_child_agent")
@patch("tools.delegate_tool._resolve_delegation_credentials")
@patch("tools.delegate_tool._load_config")
def test_mixed_batch_is_routed_per_function_before_children_are_built(
    mock_config,
    mock_resolve_credentials,
    mock_build,
    mock_run,
    _mock_live,
):
    mock_config.return_value = _routing_config()
    mock_resolve_credentials.side_effect = _resolved_credentials
    mock_build.side_effect = _child_from_build
    mock_run.side_effect = [
        {"task_index": 0, "status": "completed", "summary": "m", "_child_role": "leaf"},
        {"task_index": 1, "status": "completed", "summary": "s", "_child_role": "leaf"},
        {"task_index": 2, "status": "completed", "summary": "c", "_child_role": "leaf"},
    ]
    parent = _make_mock_parent()
    parent.model = "gpt-5.4-mini"

    result = json.loads(
        delegate_task(
            tasks=[
                {"goal": "Extract and list filenames"},
                {"goal": "Implement the API fix and tests"},
                {
                    "goal": "Review the production release gate",
                    "function_class": "mechanical",
                    # Adversarial stale payload fields. They are not in the tool
                    # schema and must never influence the runtime route.
                    "provider": "openai-codex",
                    "model": "gpt-5.4-mini",
                },
            ],
            parent_agent=parent,
        )
    )

    assert "error" not in result
    assert [entry["function_class"] for entry in result["results"]] == [
        "mechanical",
        "specialist",
        "critical",
    ]
    assert result["results"][2]["model"] == "gpt-5.6-sol"
    assert result["results"][2]["provider"] == "openai-codex"
    calls = [call.kwargs for call in mock_build.call_args_list]
    assert [call["model"] for call in calls] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert [call["functional_class"] for call in calls] == [
        "mechanical",
        "specialist",
        "critical",
    ]
    assert calls[2]["functional_inferred_class"] == "critical"
    assert calls[2]["functional_requested_class"] == "mechanical"
    assert calls[2]["functional_decision_source"] == "runtime_escalation"
    assert calls[2]["inherit_parent_fallback"] is False


@patch("tools.delegation_live_log.create_live_transcripts", return_value=(None, [], []))
@patch("tools.delegate_tool._build_child_agent")
@patch("tools.delegate_tool._resolve_delegation_credentials")
@patch("tools.delegate_tool._load_config")
def test_missing_enforced_route_fails_before_any_child_is_built(
    mock_config,
    mock_resolve_credentials,
    mock_build,
    _mock_live,
):
    config = _routing_config()
    del config["model_routing"]["routes"]["critical"]
    mock_config.return_value = config
    mock_resolve_credentials.side_effect = _resolved_credentials
    parent = _make_mock_parent()

    result = json.loads(
        delegate_task(
            goal="Approve the final production release gate",
            parent_agent=parent,
        )
    )

    assert "Missing enforced delegation model route" in result["error"]
    mock_build.assert_not_called()
    mock_resolve_credentials.assert_not_called()


@patch("tools.delegation_live_log.create_live_transcripts", return_value=(None, [], []))
@patch("tools.delegate_tool._build_child_agent")
@patch("tools.delegate_tool._load_config")
def test_post_resolution_provider_divergence_fails_before_child_build(
    mock_config,
    mock_build,
    _mock_live,
):
    config = _routing_config()
    route = config["model_routing"]["routes"]["specialist"]
    route.update({
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
        "api_mode": "codex_responses",
    })
    mock_config.return_value = config

    result = json.loads(
        delegate_task(
            goal="Implement the API fix",
            parent_agent=_make_mock_parent(),
        )
    )

    assert "route resolution diverged" in result["error"]
    assert "openai-codex:gpt-5.6-terra" in result["error"]
    assert "custom:gpt-5.6-terra" in result["error"]
    mock_build.assert_not_called()


@patch("run_agent.AIAgent")
def test_child_build_disables_parent_model_fallback_and_records_route(MockAgent):
    parent = _make_mock_parent()
    parent._fallback_chain = [{"provider": "openai-codex", "model": "gpt-5.4-mini"}]
    parent.session_id = "parent-session"
    child = MagicMock()
    child.session_id = "child-session"
    child._session_init_model_config = {}
    MockAgent.return_value = child

    built = _build_child_agent(
        task_index=0,
        goal="Review the production release gate",
        context=None,
        toolsets=None,
        model="gpt-5.6-sol",
        max_iterations=50,
        task_count=1,
        parent_agent=parent,
        override_provider="openai-codex",
        override_base_url="https://chatgpt.com/backend-api/codex",
        override_api_key="test-key",
        override_api_mode="codex_responses",
        override_reasoning_effort="xhigh",
        route_enforced=True,
        inherit_parent_fallback=False,
        functional_class="critical",
        functional_inferred_class="specialist",
        functional_trusted_inferred_class="critical",
        functional_requested_class="mechanical",
        functional_decision_source="runtime_escalation",
        role="leaf",
    )

    kwargs = MockAgent.call_args.kwargs
    assert kwargs["provider"] == "openai-codex"
    assert kwargs["model"] == "gpt-5.6-sol"
    assert kwargs["fallback_model"] is None
    assert getattr(built, "_delegate_function_class") == "critical"
    assert getattr(built, "_delegate_function_trusted_inferred") == "critical"
    assert getattr(built, "_delegate_route_model") == "gpt-5.6-sol"
    assert child._session_init_model_config["_delegate_function_class"] == "critical"
    assert (
        child._session_init_model_config["_delegate_function_trusted_inferred"]
        == "critical"
    )
    assert (
        child._session_init_model_config["_delegate_function_source"]
        == "runtime_escalation"
    )
    assert child._session_init_model_config["_delegate_route_model"] == "gpt-5.6-sol"


@patch("run_agent.OpenAI")
@patch("run_agent.check_toolset_requirements", return_value={})
@patch("run_agent.get_tool_definitions", return_value=[])
@patch("hermes_cli.config.load_config", return_value={"agent": {}, "compression": {}})
def test_real_child_agent_keeps_enforced_route_before_first_request(
    _mock_config,
    _mock_tools,
    _mock_requirements,
    mock_openai,
):
    parent = _make_mock_parent()
    parent.session_id = "parent-session"
    parent.enabled_toolsets = []
    parent.disabled_toolsets = []
    parent._fallback_chain = [{"provider": "openai-codex", "model": "gpt-5.4-mini"}]
    mock_openai.return_value = MagicMock()

    child = _build_child_agent(
        task_index=0,
        goal="Implement the API fix and tests",
        context=None,
        toolsets=None,
        model="gpt-5.6-terra",
        max_iterations=50,
        task_count=1,
        parent_agent=parent,
        override_provider="openai-codex",
        override_base_url="https://chatgpt.com/backend-api/codex",
        override_api_key="test-key",
        override_api_mode="codex_responses",
        override_reasoning_effort="high",
        route_enforced=True,
        inherit_parent_fallback=False,
        functional_class="specialist",
        functional_inferred_class="specialist",
        functional_trusted_inferred_class="specialist",
        functional_decision_source="trusted_parent_confirmed",
        role="leaf",
    )

    assert getattr(child, "provider") == "openai-codex"
    assert getattr(child, "model") == "gpt-5.6-terra"
    assert getattr(child, "api_mode") == "codex_responses"
    assert str(child.base_url).rstrip("/") == "https://chatgpt.com/backend-api/codex"
    assert getattr(child, "_fallback_chain") == []
    assert getattr(child, "_delegate_route_provider") == getattr(child, "provider")
    assert getattr(child, "_delegate_route_model") == getattr(child, "model")


@patch("run_agent.AIAgent")
def test_enforced_route_does_not_inherit_parent_secret_or_reasoning(MockAgent):
    parent = _make_mock_parent()
    parent.api_key = "parent-secret"
    parent.reasoning_config = {"enabled": True, "effort": "xhigh"}
    parent.provider = "openai-codex"
    parent.api_mode = "anthropic_messages"
    parent._fallback_chain = [{"provider": "openai-codex", "model": "gpt-5.4-mini"}]
    child = MagicMock()
    child.session_id = "child-session"
    child._session_init_model_config = {}
    MockAgent.return_value = child

    _build_child_agent(
        task_index=0,
        goal="Extract file names",
        context=None,
        toolsets=None,
        model="gpt-5.6-luna",
        max_iterations=50,
        task_count=1,
        parent_agent=parent,
        override_provider="openai-codex",
        override_base_url="https://chatgpt.com/backend-api/codex",
        override_api_key=None,
        override_api_mode=None,
        override_reasoning_effort=None,
        route_enforced=True,
        inherit_parent_fallback=False,
        functional_class="mechanical",
        role="leaf",
    )

    kwargs = MockAgent.call_args.kwargs
    assert kwargs["api_key"] is None
    assert kwargs["api_mode"] is None
    assert kwargs["reasoning_config"] is None
    assert kwargs["fallback_model"] is None


@patch("tools.delegate_tool.delegate_task", return_value="{}")
def test_registry_dispatch_forwards_function_class_and_runtime_context(mock_delegate):
    result = registry.dispatch(
        "delegate_task",
        {"goal": "List files", "function_class": "critical"},
        parent_agent=_make_mock_parent(),
        user_task="Deploy with rollback and validation",
    )

    assert result == "{}"
    kwargs = mock_delegate.call_args.kwargs
    assert kwargs["function_class"] == "critical"
    assert kwargs["trusted_parent_context"] == "Deploy with rollback and validation"


@patch("tools.delegation_live_log.create_live_transcripts", return_value=(None, [], []))
@patch(
    "tools.delegate_tool._resolve_delegation_credentials",
    side_effect=_resolved_credentials,
)
@patch("tools.delegate_tool._load_config", side_effect=lambda: _routing_config())
def test_batch_child_construction_failure_rolls_back_only_current_batch(
    _mock_config,
    _mock_credentials,
    _mock_live,
):
    parent = _make_mock_parent()
    existing = MagicMock()
    existing._delegate_construction_batch_id = "other-batch"
    parent._active_children = [existing]
    built = []

    def _failing_build(**kwargs):
        child = MagicMock()
        child._delegate_construction_batch_id = kwargs["construction_batch_id"]
        parent._active_children.append(child)
        built.append(child)
        if len(built) == 2:
            raise RuntimeError("second child construction failed")
        return child

    with patch("tools.delegate_tool._build_child_agent", side_effect=_failing_build):
        with pytest.raises(RuntimeError, match="second child construction failed"):
            delegate_task(
                tasks=[
                    {"goal": "Extract files"},
                    {"goal": "Implement fix"},
                ],
                parent_agent=parent,
            )

    assert parent._active_children == [existing]
