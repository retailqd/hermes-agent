from __future__ import annotations

import pytest

from agent.functional_model_routing import (
    infer_function_class,
    normalize_function_class,
    resolve_functional_model_route,
)


def _routing_config():
    return {
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
        "reasoning_effort": "low",
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


@pytest.mark.parametrize(
    ("goal", "role", "expected"),
    [
        ("Extract and list all file names", "leaf", "mechanical"),
        ("Implement the backend fix and tests", "leaf", "specialist"),
        ("Coordinate three independent workstreams", "leaf", "coordinator"),
        ("Prepare a harmless plan", "orchestrator", "coordinator"),
        ("Perform the final production release gate", "leaf", "critical"),
        ("Deploy production", "leaf", "critical"),
        ("Handle this task", "leaf", "specialist"),
        ("Monitorar respostas e listar decisões", "leaf", "mechanical"),
        ("Revisar o deploy em produção", "leaf", "critical"),
    ],
)
def test_infer_function_class(goal, role, expected):
    assert infer_function_class(goal, role=role) == expected


def test_requested_class_can_escalate_mechanical_task():
    route = resolve_functional_model_route(
        _routing_config(),
        goal="Extract and list all file names",
        requested_class="coordinator",
    )

    assert route.function_class == "coordinator"
    assert route.inferred_class == "mechanical"
    assert route.decision_source == "requested_escalation"
    assert route.credential_config["model"] == "gpt-5.6-sol"


def test_requested_class_cannot_downgrade_release_gate():
    route = resolve_functional_model_route(
        _routing_config(),
        goal="Review the production release gate and rollback plan",
        requested_class="mechanical",
    )

    assert route.function_class == "critical"
    assert route.inferred_class == "critical"
    assert route.decision_source == "runtime_escalation"
    assert route.credential_config["model"] == "gpt-5.6-sol"
    assert route.inherit_parent_fallback is False


def test_enabled_route_overrides_legacy_global_model_without_transport_leakage():
    config = _routing_config()
    config.update({
        "base_url": "https://legacy.invalid/v1",
        "api_key": "legacy-secret",
        "api_mode": "anthropic_messages",
    })
    route = resolve_functional_model_route(
        config,
        goal="Implement a typed API client",
    )

    assert route.enabled is True
    assert route.function_class == "specialist"
    assert route.credential_config["provider"] == "openai-codex"
    assert route.credential_config["model"] == "gpt-5.6-terra"
    assert route.credential_config["model"] != "gpt-5.4-mini"
    assert "base_url" not in route.credential_config
    assert "api_key" not in route.credential_config
    assert "api_mode" not in route.credential_config
    assert route.reasoning_effort == "high"


def test_disabled_routing_preserves_legacy_credentials_and_fallback():
    config = {
        "provider": "openrouter",
        "model": "legacy/model",
        "reasoning_effort": "medium",
        "model_routing": {"enabled": False},
    }
    route = resolve_functional_model_route(config, goal="Implement the fix")

    assert route.enabled is False
    assert route.credential_config["provider"] == "openrouter"
    assert route.credential_config["model"] == "legacy/model"
    assert route.inherit_parent_fallback is True


def test_enabled_routing_fails_closed_when_selected_route_is_missing():
    config = _routing_config()
    del config["model_routing"]["routes"]["critical"]

    with pytest.raises(ValueError, match="Missing enforced delegation model route"):
        resolve_functional_model_route(
            config,
            goal="Approve the final production release gate",
        )


def test_enabled_routing_fails_closed_when_provider_or_model_is_empty():
    config = _routing_config()
    config["model_routing"]["routes"]["specialist"]["model"] = ""

    with pytest.raises(ValueError, match="must define non-empty provider and model"):
        resolve_functional_model_route(config, goal="Implement the fix")


def test_invalid_requested_class_is_rejected():
    with pytest.raises(ValueError, match="Unknown delegation function_class"):
        resolve_functional_model_route(
            _routing_config(),
            goal="Do work",
            requested_class="cheap-model",
        )


def test_malformed_model_routing_config_is_rejected_instead_of_bypassed():
    with pytest.raises(ValueError, match="delegation.model_routing must be a mapping"):
        resolve_functional_model_route(
            {"model": "gpt-5.4-mini", "model_routing": "enabled"},
            goal="Review the production release gate",
        )


def test_trusted_parent_context_sets_non_downgradeable_floor():
    route = resolve_functional_model_route(
        _routing_config(),
        goal="Extract and list all file names",
        requested_class="mechanical",
        trusted_parent_context="Approve the production release gate and rollback plan",
    )

    assert route.function_class == "critical"
    assert route.inferred_class == "mechanical"
    assert route.trusted_inferred_class == "critical"
    assert route.decision_source == "trusted_parent_escalation"
    assert route.credential_config["model"] == "gpt-5.6-sol"


def test_enforced_route_rejects_parent_fallback_even_if_config_requests_it():
    config = _routing_config()
    config["model_routing"]["inherit_parent_fallback"] = True

    with pytest.raises(ValueError, match="inherit_parent_fallback=true is unsafe"):
        resolve_functional_model_route(config, goal="Implement API fix")


def test_aliases_do_not_expose_provider_or_model_selection():
    assert normalize_function_class("release_gate") == "critical"
    assert normalize_function_class("orchestrator") == "coordinator"
    assert normalize_function_class("implementation") == "specialist"
