from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from .contracts import (
    CLEANUP_PENDING_STATE,
    GateDecision,
    Lifecycle,
    TERMINAL_LIFECYCLES,
    validate_mattermost_id,
    validate_task_id,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _require_utc(dt: datetime, field_name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must be UTC-aware")
    return dt.astimezone(UTC)


def _format_utc(dt: datetime) -> str:
    return _require_utc(dt, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _require_text(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


def _require_non_negative_int(value: int, field_name: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return number


@dataclass(frozen=True, slots=True)
class MattermostCockpitTask:
    task_id: str
    team_id: str
    source_channel_id: str
    source_root_id: str
    source_post_id: str
    owner_author_id: str
    executions_channel_id: str
    execution_root_id: str | None = None
    execution_permalink: str | None = None
    watcher_user_id: str = ""
    title: str = ""
    lifecycle: Lifecycle = Lifecycle.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    closed_at: datetime | None = None
    source_cursor_ms: int = 0
    execution_cursor_ms: int = 0
    watcher_owner: str | None = None
    watcher_heartbeat_at: datetime | None = None
    result_summary: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    cleanup_state: str | None = None
    pending_outcome: Lifecycle | None = None
    dedupe_key: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", validate_task_id(self.task_id))
        object.__setattr__(self, "team_id", validate_mattermost_id(self.team_id, "team_id"))
        object.__setattr__(self, "source_channel_id", validate_mattermost_id(self.source_channel_id, "source_channel_id"))
        object.__setattr__(self, "source_root_id", validate_mattermost_id(self.source_root_id, "source_root_id"))
        object.__setattr__(self, "source_post_id", validate_mattermost_id(self.source_post_id, "source_post_id"))
        object.__setattr__(self, "owner_author_id", validate_mattermost_id(self.owner_author_id, "owner_author_id"))
        object.__setattr__(self, "executions_channel_id", validate_mattermost_id(self.executions_channel_id, "executions_channel_id"))
        object.__setattr__(self, "watcher_user_id", validate_mattermost_id(self.watcher_user_id, "watcher_user_id"))
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        if self.execution_root_id is not None:
            object.__setattr__(self, "execution_root_id", validate_mattermost_id(self.execution_root_id, "execution_root_id"))
        if self.execution_permalink is not None:
            object.__setattr__(self, "execution_permalink", _require_text(self.execution_permalink, "execution_permalink"))
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _require_utc(self.updated_at, "updated_at"))
        if self.closed_at is not None:
            object.__setattr__(self, "closed_at", _require_utc(self.closed_at, "closed_at"))
        if self.watcher_heartbeat_at is not None:
            object.__setattr__(self, "watcher_heartbeat_at", _require_utc(self.watcher_heartbeat_at, "watcher_heartbeat_at"))
        if self.watcher_owner is not None:
            object.__setattr__(self, "watcher_owner", _require_text(self.watcher_owner, "watcher_owner"))
        if self.result_summary is not None:
            object.__setattr__(self, "result_summary", _require_text(self.result_summary, "result_summary"))
        if self.last_error is not None:
            object.__setattr__(self, "last_error", _require_text(self.last_error, "last_error"))
        if self.cleanup_state is not None:
            cleanup_state = _require_text(self.cleanup_state, "cleanup_state")
            if cleanup_state != CLEANUP_PENDING_STATE:
                raise ValueError(f"cleanup_state must be {CLEANUP_PENDING_STATE!r}")
            object.__setattr__(self, "cleanup_state", cleanup_state)
        if self.pending_outcome is not None:
            if not isinstance(self.pending_outcome, Lifecycle):
                object.__setattr__(self, "pending_outcome", Lifecycle(str(self.pending_outcome)))
            if self.pending_outcome not in TERMINAL_LIFECYCLES:
                raise ValueError("pending_outcome must be terminal")
        if (self.cleanup_state is None) != (self.pending_outcome is None):
            raise ValueError("cleanup_state and pending_outcome must be set together")
        object.__setattr__(self, "evidence", dict(self.evidence or {}))
        object.__setattr__(self, "source_cursor_ms", _require_non_negative_int(self.source_cursor_ms, "source_cursor_ms"))
        object.__setattr__(self, "execution_cursor_ms", _require_non_negative_int(self.execution_cursor_ms, "execution_cursor_ms"))
        object.__setattr__(self, "version", _require_non_negative_int(self.version, "version"))
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if self.lifecycle in TERMINAL_LIFECYCLES:
            if self.closed_at is None:
                raise ValueError("closed_at is required for terminal lifecycle")
        elif self.closed_at is not None:
            raise ValueError("nonterminal lifecycle must not carry closed_at")
        if self.lifecycle is Lifecycle.SUCCEEDED:
            if self.result_summary is None:
                raise ValueError("result_summary is required for succeeded tasks")
            if not self.evidence:
                raise ValueError("evidence is required for succeeded tasks")

    def creation_binding(self) -> tuple[Any, ...]:
        return (
            self.team_id,
            self.source_channel_id,
            self.source_root_id,
            self.source_post_id,
            self.owner_author_id,
            self.executions_channel_id,
            self.watcher_user_id,
            self.title,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "team_id": self.team_id,
            "source_channel_id": self.source_channel_id,
            "source_root_id": self.source_root_id,
            "source_post_id": self.source_post_id,
            "owner_author_id": self.owner_author_id,
            "executions_channel_id": self.executions_channel_id,
            "execution_root_id": self.execution_root_id,
            "execution_permalink": self.execution_permalink,
            "watcher_user_id": self.watcher_user_id,
            "title": self.title,
            "lifecycle": self.lifecycle.value,
            "created_at": _format_utc(self.created_at),
            "updated_at": _format_utc(self.updated_at),
            "closed_at": _format_utc(self.closed_at) if self.closed_at else None,
            "source_cursor_ms": self.source_cursor_ms,
            "execution_cursor_ms": self.execution_cursor_ms,
            "watcher_owner": self.watcher_owner,
            "watcher_heartbeat_at": _format_utc(self.watcher_heartbeat_at) if self.watcher_heartbeat_at else None,
            "result_summary": self.result_summary,
            "evidence_json": json.dumps(self.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            "last_error": self.last_error,
            "cleanup_state": self.cleanup_state,
            "pending_outcome": self.pending_outcome.value if self.pending_outcome is not None else None,
            "dedupe_key": self.dedupe_key,
            "version": self.version,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MattermostCockpitTask":
        created_at = _parse_utc(row["created_at"])
        updated_at = _parse_utc(row["updated_at"])
        if created_at is None or updated_at is None:
            raise ValueError("task row timestamps must be present")
        evidence_json = row["evidence_json"]
        if evidence_json in (None, ""):
            evidence = {}
        else:
            evidence = json.loads(evidence_json)
        return cls(
            task_id=row["task_id"],
            team_id=row["team_id"],
            source_channel_id=row["source_channel_id"],
            source_root_id=row["source_root_id"],
            source_post_id=row["source_post_id"],
            owner_author_id=row["owner_author_id"],
            executions_channel_id=row["executions_channel_id"],
            execution_root_id=row["execution_root_id"],
            execution_permalink=row["execution_permalink"],
            watcher_user_id=row["watcher_user_id"],
            title=row["title"],
            lifecycle=Lifecycle(row["lifecycle"]),
            created_at=created_at,
            updated_at=updated_at,
            closed_at=_parse_utc(row["closed_at"]),
            source_cursor_ms=row["source_cursor_ms"],
            execution_cursor_ms=row["execution_cursor_ms"],
            watcher_owner=row["watcher_owner"],
            watcher_heartbeat_at=_parse_utc(row["watcher_heartbeat_at"]),
            result_summary=row["result_summary"],
            evidence=evidence,
            last_error=row["last_error"],
            cleanup_state=row.get("cleanup_state") if hasattr(row, "get") else row["cleanup_state"],
            pending_outcome=(
                Lifecycle(row.get("pending_outcome"))
                if hasattr(row, "get") and row.get("pending_outcome")
                else Lifecycle(row["pending_outcome"])
                if not hasattr(row, "get") and row["pending_outcome"]
                else None
            ),
            dedupe_key=row["dedupe_key"],
            version=row["version"],
        )


@dataclass(frozen=True, slots=True)
class MattermostCockpitAuditEvent:
    task_id: str
    event_type: str
    actor_user_id: str
    created_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", validate_task_id(self.task_id))
        object.__setattr__(self, "event_type", _require_text(self.event_type, "event_type"))
        object.__setattr__(self, "actor_user_id", validate_mattermost_id(self.actor_user_id, "actor_user_id"))
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "payload", dict(self.payload or {}))
        if self.dedupe_key is not None:
            object.__setattr__(self, "dedupe_key", _require_text(self.dedupe_key, "dedupe_key"))

    def to_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "event_type": self.event_type,
            "actor_user_id": self.actor_user_id,
            "created_at": _format_utc(self.created_at),
            "payload_json": json.dumps(self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            "dedupe_key": self.dedupe_key,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MattermostCockpitAuditEvent":
        created_at = _parse_utc(row["created_at"])
        if created_at is None:
            raise ValueError("audit event timestamps must be present")
        payload_json = row["payload_json"]
        return cls(
            task_id=row["task_id"],
            event_type=row["event_type"],
            actor_user_id=row["actor_user_id"],
            created_at=created_at,
            payload=json.loads(payload_json) if payload_json else {},
            dedupe_key=row["dedupe_key"],
        )


@dataclass(frozen=True, slots=True)
class MattermostCockpitGateRelay:
    gate_id: str
    task_id: str
    prompt_post_id: str
    prompt_body: str
    decision: GateDecision | None = None
    source_owner_post_id: str | None = None
    source_body: str | None = None
    destination_post_id: str | None = None
    destination_body: str | None = None
    active: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_id", validate_task_id(self.gate_id))
        object.__setattr__(self, "task_id", validate_task_id(self.task_id))
        object.__setattr__(self, "prompt_post_id", validate_mattermost_id(self.prompt_post_id, "prompt_post_id"))
        object.__setattr__(self, "prompt_body", _require_text(self.prompt_body, "prompt_body"))
        if self.decision is not None and not isinstance(self.decision, GateDecision):
            object.__setattr__(self, "decision", GateDecision(str(self.decision)))
        if self.source_owner_post_id is not None:
            object.__setattr__(self, "source_owner_post_id", validate_mattermost_id(self.source_owner_post_id, "source_owner_post_id"))
        if self.source_body is not None:
            object.__setattr__(self, "source_body", _require_text(self.source_body, "source_body"))
        if self.destination_post_id is not None:
            object.__setattr__(self, "destination_post_id", validate_mattermost_id(self.destination_post_id, "destination_post_id"))
        if self.destination_body is not None:
            object.__setattr__(self, "destination_body", _require_text(self.destination_body, "destination_body"))
        object.__setattr__(self, "active", bool(self.active))
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _require_utc(self.updated_at, "updated_at"))
        if self.last_error is not None:
            object.__setattr__(self, "last_error", _require_text(self.last_error, "last_error"))
        if self.active and any((self.decision, self.source_owner_post_id, self.destination_post_id)):
            raise ValueError("active gate must not carry a resolved decision")
        if not self.active and not all((self.decision, self.source_owner_post_id, self.source_body, self.destination_post_id, self.destination_body)):
            raise ValueError("resolved gate requires decision and both source/destination bindings")

    def to_record(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "task_id": self.task_id,
            "prompt_post_id": self.prompt_post_id,
            "prompt_body": self.prompt_body,
            "decision": self.decision.value if self.decision is not None else None,
            "source_owner_post_id": self.source_owner_post_id,
            "source_body": self.source_body,
            "destination_post_id": self.destination_post_id,
            "destination_body": self.destination_body,
            "active": 1 if self.active else 0,
            "created_at": _format_utc(self.created_at),
            "updated_at": _format_utc(self.updated_at),
            "last_error": self.last_error,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MattermostCockpitGateRelay":
        created_at = _parse_utc(row["created_at"])
        updated_at = _parse_utc(row["updated_at"])
        if created_at is None or updated_at is None:
            raise ValueError("gate relay row timestamps must be present")
        return cls(
            gate_id=row["gate_id"],
            task_id=row["task_id"],
            prompt_post_id=row["prompt_post_id"],
            prompt_body=row["prompt_body"],
            decision=GateDecision(row["decision"]) if row["decision"] else None,
            source_owner_post_id=row["source_owner_post_id"],
            source_body=row["source_body"],
            destination_post_id=row["destination_post_id"],
            destination_body=row["destination_body"],
            active=bool(row["active"]),
            created_at=created_at,
            updated_at=updated_at,
            last_error=row["last_error"],
        )
