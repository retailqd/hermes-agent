"""Deterministic function-based model routing for delegated agents.

The model may request a functional class, but it never selects a provider or
model. The runtime infers a minimum class from the task and only permits a
request to keep or raise that class. When routing is enabled, every selected
route must define an explicit provider and model. Missing routes fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Optional

from utils import is_truthy_value


FUNCTION_CLASSES = ("mechanical", "specialist", "coordinator", "critical")
_CLASS_RANK = {name: rank for rank, name in enumerate(FUNCTION_CLASSES)}


# Ordered from strongest to weakest. Critical and coordinator signals are
# checked before specialist and mechanical signals so a task such as
# "review the production release" cannot be downgraded by the word "review".
_CRITICAL_RE = re.compile(
    r"\b(?:"
    r"release[ -]?gate|final release review|go[ /-]?no[ -]?go|"
    r"production (?:release|deploy(?:ment)?|rollback|migration|cutover)|"
    r"prod(?:uction)? deploy(?:ment)?|deploy(?:ment)? (?:to|in|on) prod(?:uction)?|"
    r"deploy(?:ment)? prod(?:uction)?|"
    r"deploy em produ[cç][aã]o|gate de release|rollback|cutover|"
    r"schema migration|database migration|migra[cç][aã]o|"
    r"credential(?:s)?|secret(?:s)?|env(?:ironment)? var(?:iable)?s?"
    r")\b",
    re.IGNORECASE,
)
_COORDINATOR_RE = re.compile(
    r"\b(?:"
    r"coordinat(?:e|or|ion)|orchestrat(?:e|or|ion)|"
    r"decompos(?:e|ition)|supervis(?:e|ion)|"
    r"multi[ -]agent|workstreams?|portfolio|"
    r"coorden(?:ar|e|a[cç][aã]o)|orquestr(?:ar|e|a[cç][aã]o)|"
    r"decompor|supervisionar|frentes? de trabalho"
    r")\b",
    re.IGNORECASE,
)
_SPECIALIST_RE = re.compile(
    r"\b(?:"
    r"implement(?:ation|ing|ed)?|fix(?:ing|ed)?|debug(?:ging|ged)?|"
    r"review(?:ing|ed)?|research(?:ing|ed)?|analy[sz](?:e|ing|ed|is)|"
    r"architect(?:ure|ural)?|design|develop(?:ment|ing|ed)?|"
    r"refactor(?:ing|ed)?|test(?:ing|ed)?|code|security|threat|"
    r"investigat(?:e|ing|ion)|diagnos(?:e|ing|is|tic)|"
    r"implementar|corrigir|depurar|revis(?:ar|[aã]o)|pesquisar|"
    r"analis(?:ar|e)|arquitetura|desenvolver|refatorar|testar|"
    r"seguran[cç]a|investigar|diagnosticar"
    r")\b",
    re.IGNORECASE,
)
_MECHANICAL_RE = re.compile(
    r"\b(?:"
    r"extract(?:ion|ing|ed)?|list(?:ing|ed)?|poll(?:ing|ed)?|"
    r"monitor(?:ing|ed)?|collect(?:ing|ed)?|format(?:ting|ted)?|"
    r"rename|transcrib(?:e|ing|ed)|read[ -]?back|status check|"
    r"inspect(?:ion|ing|ed)?|inventory|enumerat(?:e|ing|ion)|"
    r"extrair|listar|monitorar|coletar|formatar|renomear|"
    r"transcrever|inspecionar|inventariar|enumerar"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FunctionalModelRoute:
    """Resolved, auditable routing decision for one delegated task."""

    enabled: bool
    function_class: str
    inferred_class: str
    trusted_inferred_class: Optional[str]
    requested_class: Optional[str]
    decision_source: str
    credential_config: Mapping[str, Any]
    reasoning_effort: Any = None
    inherit_parent_fallback: bool = True


def normalize_function_class(value: Optional[str]) -> Optional[str]:
    """Return a canonical functional class or raise for an invalid value."""

    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "release_gate": "critical",
        "gate": "critical",
        "reviewer": "specialist",
        "implementation": "specialist",
        "implementer": "specialist",
        "worker": "specialist",
        "mechanic": "mechanical",
        "monitor": "mechanical",
        "orchestrator": "coordinator",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _CLASS_RANK:
        allowed = ", ".join(FUNCTION_CLASSES)
        raise ValueError(
            f"Unknown delegation function_class {value!r}. Allowed values: {allowed}."
        )
    return normalized


def infer_function_class(
    goal: str,
    context: Optional[str] = None,
    *,
    role: str = "leaf",
    default_class: str = "specialist",
) -> str:
    """Infer the minimum safe class from task text and structural role."""

    normalized_default = normalize_function_class(default_class) or "specialist"
    text = f"{goal or ''}\n{context or ''}"

    if _CRITICAL_RE.search(text):
        inferred = "critical"
    elif str(role or "leaf").strip().lower() == "orchestrator":
        inferred = "coordinator"
    elif _COORDINATOR_RE.search(text):
        inferred = "coordinator"
    elif _SPECIALIST_RE.search(text):
        inferred = "specialist"
    elif _MECHANICAL_RE.search(text):
        inferred = "mechanical"
    else:
        inferred = normalized_default

    # default_class applies only when no signal matched. It is not a global
    # floor, so an obvious extraction or monitoring task can still use the
    # mechanical route while ambiguous work defaults to specialist.
    return inferred


def _decision_source(
    inferred: str,
    requested: Optional[str],
    *,
    trusted_parent_escalated: bool = False,
) -> str:
    if requested is None:
        return (
            "trusted_parent_escalation"
            if trusted_parent_escalated
            else "runtime_inference"
        )
    if _CLASS_RANK[requested] < _CLASS_RANK[inferred]:
        return (
            "trusted_parent_escalation"
            if trusted_parent_escalated
            else "runtime_escalation"
        )
    if requested == inferred:
        return (
            "trusted_parent_confirmed"
            if trusted_parent_escalated
            else "requested_confirmed"
        )
    return "requested_escalation"


def resolve_functional_model_route(
    delegation_config: Mapping[str, Any],
    *,
    goal: str,
    context: Optional[str] = None,
    requested_class: Optional[str] = None,
    role: str = "leaf",
    trusted_parent_context: Optional[str] = None,
) -> FunctionalModelRoute:
    """Resolve one task to an operator-configured provider/model route.

    Disabled routing preserves the legacy delegation config exactly. Enabled
    routing is strict: the chosen route must exist and contain both provider
    and model. The caller cannot request either value directly.
    """

    routing_value = delegation_config.get("model_routing")
    if routing_value is None:
        routing: Mapping[str, Any] = {}
    elif isinstance(routing_value, Mapping):
        routing = routing_value
    else:
        raise ValueError("delegation.model_routing must be a mapping.")

    if not is_truthy_value(routing.get("enabled", False)):
        requested = normalize_function_class(requested_class)
        payload_inferred = infer_function_class(
            goal,
            context,
            role=role,
            default_class="specialist",
        )
        trusted_inferred = (
            infer_function_class(
                trusted_parent_context,
                role="leaf",
                default_class="specialist",
            )
            if isinstance(trusted_parent_context, str)
            and trusted_parent_context.strip()
            else None
        )
        trusted_parent_escalated = bool(
            trusted_inferred
            and _CLASS_RANK[trusted_inferred] > _CLASS_RANK[payload_inferred]
        )
        runtime_floor = payload_inferred
        if trusted_parent_escalated:
            assert trusted_inferred is not None
            runtime_floor = trusted_inferred
        effective = runtime_floor
        if requested and _CLASS_RANK[requested] > _CLASS_RANK[runtime_floor]:
            effective = requested
        return FunctionalModelRoute(
            enabled=False,
            function_class=effective,
            inferred_class=payload_inferred,
            trusted_inferred_class=trusted_inferred,
            requested_class=requested,
            decision_source=_decision_source(
                runtime_floor,
                requested,
                trusted_parent_escalated=trusted_parent_escalated,
            ),
            credential_config=dict(delegation_config),
            reasoning_effort=delegation_config.get("reasoning_effort"),
            inherit_parent_fallback=True,
        )

    default_class = (
        normalize_function_class(routing.get("default_class") or "specialist")
        or "specialist"
    )
    requested = normalize_function_class(requested_class)
    payload_inferred = infer_function_class(
        goal,
        context,
        role=role,
        default_class=default_class,
    )
    trusted_inferred = (
        infer_function_class(
            trusted_parent_context,
            role="leaf",
            default_class=default_class,
        )
        if isinstance(trusted_parent_context, str) and trusted_parent_context.strip()
        else None
    )
    trusted_parent_escalated = bool(
        trusted_inferred
        and _CLASS_RANK[trusted_inferred] > _CLASS_RANK[payload_inferred]
    )
    runtime_floor = payload_inferred
    if trusted_parent_escalated:
        assert trusted_inferred is not None
        runtime_floor = trusted_inferred
    effective = runtime_floor
    if requested and _CLASS_RANK[requested] > _CLASS_RANK[runtime_floor]:
        effective = requested

    routes = routing.get("routes")
    if not isinstance(routes, Mapping):
        raise ValueError(
            "delegation.model_routing.enabled=true requires a routes mapping."
        )
    route = routes.get(effective)
    if not isinstance(route, Mapping):
        raise ValueError(
            f"Missing enforced delegation model route for function_class {effective!r}."
        )

    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    if not provider or not model:
        raise ValueError(
            "Enforced delegation route "
            f"{effective!r} must define non-empty provider and model."
        )

    # Enforced routes are self-contained. Do not inherit legacy provider,
    # model, endpoint, transport, API key, or request overrides from the
    # top-level delegation block. That prevents a stale global setting from
    # silently changing the wire path for a functional route. Every route key
    # remains available to credential resolution, including custom endpoint
    # and request override fields.
    credential_config = dict(route)
    credential_config["provider"] = provider
    credential_config["model"] = model

    reasoning_effort = route.get("reasoning_effort")

    if is_truthy_value(routing.get("inherit_parent_fallback", False), default=False):
        raise ValueError(
            "delegation.model_routing.inherit_parent_fallback=true is unsafe for "
            "enforced routes because fallback activation cannot yet be audited "
            "as the effective provider/model. Keep it false."
        )

    return FunctionalModelRoute(
        enabled=True,
        function_class=effective,
        inferred_class=payload_inferred,
        trusted_inferred_class=trusted_inferred,
        requested_class=requested,
        decision_source=_decision_source(
            runtime_floor,
            requested,
            trusted_parent_escalated=trusted_parent_escalated,
        ),
        credential_config=credential_config,
        reasoning_effort=reasoning_effort,
        inherit_parent_fallback=False,
    )
