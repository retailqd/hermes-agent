from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Lifecycle(StrEnum):
    """Allowed lifecycle states for a cockpit task."""

    OPEN = "OPEN"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class MattermostCockpitContracts:
    """Authoritative Mattermost IDs supplied by config or env."""

    team_id: str
    main_channel_id: str
    executions_channel_id: str
    owner_author_id: str
    watcher_user_id: str

    def _validate_exact(self, field_name: str, expected: str, actual: str) -> str:
        if actual != expected:
            raise ValueError(f"{field_name} must be {expected!r}; got {actual!r}")
        return actual

    def validate_team_id(self, actual: str) -> str:
        return self._validate_exact("team_id", self.team_id, actual)

    def validate_main_channel_id(self, actual: str) -> str:
        return self._validate_exact("main_channel_id", self.main_channel_id, actual)

    def validate_executions_channel_id(self, actual: str) -> str:
        return self._validate_exact("executions_channel_id", self.executions_channel_id, actual)

    def validate_owner_author_id(self, actual: str) -> str:
        return self._validate_exact("owner_author_id", self.owner_author_id, actual)

    def validate_watcher_user_id(self, actual: str) -> str:
        return self._validate_exact("watcher_user_id", self.watcher_user_id, actual)
