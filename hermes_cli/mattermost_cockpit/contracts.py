from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MATTERMOST_ID_PATTERN = re.compile(r"^[a-z0-9]{26}$")
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Lifecycle(StrEnum):
    """Allowed lifecycle states for a cockpit task."""

    OPEN = "OPEN"
    RUNNING = "RUNNING"
    WAITING_OWNER = "WAITING_OWNER"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_LIFECYCLES = frozenset({Lifecycle.SUCCEEDED, Lifecycle.FAILED, Lifecycle.CANCELLED})
NONTERMINAL_LIFECYCLES = frozenset({Lifecycle.OPEN, Lifecycle.RUNNING, Lifecycle.WAITING_OWNER, Lifecycle.BLOCKED})
LEGAL_TRANSITIONS: dict[Lifecycle, frozenset[Lifecycle]] = {
    Lifecycle.OPEN: frozenset({Lifecycle.RUNNING, Lifecycle.FAILED, Lifecycle.CANCELLED}),
    Lifecycle.RUNNING: frozenset({Lifecycle.WAITING_OWNER, Lifecycle.BLOCKED, Lifecycle.SUCCEEDED, Lifecycle.FAILED, Lifecycle.CANCELLED}),
    Lifecycle.WAITING_OWNER: frozenset({Lifecycle.RUNNING, Lifecycle.BLOCKED, Lifecycle.FAILED, Lifecycle.CANCELLED}),
    Lifecycle.BLOCKED: frozenset({Lifecycle.RUNNING, Lifecycle.FAILED, Lifecycle.CANCELLED}),
    Lifecycle.SUCCEEDED: frozenset(),
    Lifecycle.FAILED: frozenset(),
    Lifecycle.CANCELLED: frozenset(),
}


def validate_mattermost_id(value: str, field_name: str) -> str:
    text = str(value)
    if not MATTERMOST_ID_PATTERN.fullmatch(text):
        raise ValueError(f"{field_name} must be 26 lowercase alphanumeric")
    return text


def validate_task_id(value: str) -> str:
    text = str(value)
    if not TASK_ID_PATTERN.fullmatch(text):
        raise ValueError("task_id must match safe bounded regex")
    return text


@dataclass(frozen=True, slots=True)
class MattermostCockpitContracts:
    """Authoritative Mattermost IDs supplied by config or env."""

    team_id: str
    main_channel_id: str
    executions_channel_id: str
    owner_author_id: str
    watcher_user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", validate_mattermost_id(self.team_id, "team_id"))
        object.__setattr__(self, "main_channel_id", validate_mattermost_id(self.main_channel_id, "main_channel_id"))
        object.__setattr__(self, "executions_channel_id", validate_mattermost_id(
            self.executions_channel_id,
            "executions_channel_id",
        ))
        object.__setattr__(self, "owner_author_id", validate_mattermost_id(self.owner_author_id, "owner_author_id"))
        object.__setattr__(self, "watcher_user_id", validate_mattermost_id(self.watcher_user_id, "watcher_user_id"))

    def _validate_exact(self, field_name: str, expected: str, actual: str) -> str:
        actual = validate_mattermost_id(actual, field_name)
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
