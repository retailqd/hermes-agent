from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from .contracts import Lifecycle


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
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


@dataclass(frozen=True, slots=True)
class MattermostCockpitTask:
    root_id: str
    team_id: str
    channel_id: str
    owner_author_id: str
    watcher_user_id: str
    lifecycle: Lifecycle = Lifecycle.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    closed_at: datetime | None = None
    dedupe_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_id", _require_text(self.root_id, "root_id"))
        object.__setattr__(self, "team_id", _require_text(self.team_id, "team_id"))
        object.__setattr__(self, "channel_id", _require_text(self.channel_id, "channel_id"))
        object.__setattr__(self, "owner_author_id", _require_text(self.owner_author_id, "owner_author_id"))
        object.__setattr__(self, "watcher_user_id", _require_text(self.watcher_user_id, "watcher_user_id"))
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _require_utc(self.updated_at, "updated_at"))
        if self.closed_at is not None:
            object.__setattr__(self, "closed_at", _require_utc(self.closed_at, "closed_at"))
        if self.lifecycle is Lifecycle.CLOSED and self.closed_at is None:
            raise ValueError("closed tasks must have closed_at")
        if self.dedupe_key is not None:
            object.__setattr__(self, "dedupe_key", _require_text(self.dedupe_key, "dedupe_key"))

    def to_record(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "owner_author_id": self.owner_author_id,
            "watcher_user_id": self.watcher_user_id,
            "lifecycle": self.lifecycle.value,
            "created_at": _format_utc(self.created_at),
            "updated_at": _format_utc(self.updated_at),
            "closed_at": _format_utc(self.closed_at) if self.closed_at else None,
            "dedupe_key": self.dedupe_key,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MattermostCockpitTask":
        created_at = _parse_utc(row["created_at"])
        updated_at = _parse_utc(row["updated_at"])
        if created_at is None or updated_at is None:
            raise ValueError("task row timestamps must be present")
        return cls(
            root_id=row["root_id"],
            team_id=row["team_id"],
            channel_id=row["channel_id"],
            owner_author_id=row["owner_author_id"],
            watcher_user_id=row["watcher_user_id"],
            lifecycle=Lifecycle(row["lifecycle"]),
            created_at=created_at,
            updated_at=updated_at,
            closed_at=_parse_utc(row["closed_at"]),
            dedupe_key=row.get("dedupe_key") if hasattr(row, "get") else row["dedupe_key"],
        )


@dataclass(frozen=True, slots=True)
class MattermostCockpitAuditEvent:
    root_id: str
    event_type: str
    actor_user_id: str
    created_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_id", _require_text(self.root_id, "root_id"))
        object.__setattr__(self, "event_type", _require_text(self.event_type, "event_type"))
        object.__setattr__(self, "actor_user_id", _require_text(self.actor_user_id, "actor_user_id"))
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "payload", dict(self.payload or {}))
        if self.dedupe_key is not None:
            object.__setattr__(self, "dedupe_key", _require_text(self.dedupe_key, "dedupe_key"))

    def to_record(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "event_type": self.event_type,
            "actor_user_id": self.actor_user_id,
            "created_at": _format_utc(self.created_at),
            "payload_json": json.dumps(self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            "dedupe_key": self.dedupe_key,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MattermostCockpitAuditEvent":
        payload_json = row["payload_json"]
        created_at = _parse_utc(row["created_at"])
        if created_at is None:
            raise ValueError("audit event timestamps must be present")
        return cls(
            root_id=row["root_id"],
            event_type=row["event_type"],
            actor_user_id=row["actor_user_id"],
            created_at=created_at,
            payload=json.loads(payload_json) if payload_json else {},
            dedupe_key=row.get("dedupe_key") if hasattr(row, "get") else row["dedupe_key"],
        )
