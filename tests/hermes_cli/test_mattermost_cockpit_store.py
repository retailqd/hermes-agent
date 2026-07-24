from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from hermes_cli.mattermost_cockpit.contracts import Lifecycle, MattermostCockpitContracts
from hermes_cli.mattermost_cockpit.models import (
    MattermostCockpitAuditEvent,
    MattermostCockpitTask,
)
from hermes_cli.mattermost_cockpit.store import MattermostCockpitStore


@pytest.fixture()
def contracts() -> MattermostCockpitContracts:
    return MattermostCockpitContracts(
        team_id="team-123",
        main_channel_id="chan-main",
        executions_channel_id="chan-exec",
        owner_author_id="user-owner",
        watcher_user_id="user-watcher",
    )


@pytest.fixture()
def store(tmp_path: Path, contracts: MattermostCockpitContracts) -> MattermostCockpitStore:
    return MattermostCockpitStore(db_path=tmp_path / "state.db", contracts=contracts)


@pytest.fixture()
def utc_now() -> datetime:
    return datetime(2026, 7, 24, 12, 34, 56, tzinfo=timezone.utc)


def _task(
    *,
    root_id: str = "root-1",
    lifecycle: Lifecycle = Lifecycle.OPEN,
    dedupe_key: str | None = None,
    created_at: datetime,
    updated_at: datetime,
) -> MattermostCockpitTask:
    return MattermostCockpitTask(
        root_id=root_id,
        team_id="team-123",
        channel_id="chan-main",
        owner_author_id="user-owner",
        watcher_user_id="user-watcher",
        lifecycle=lifecycle,
        created_at=created_at,
        updated_at=updated_at,
        closed_at=updated_at if lifecycle is Lifecycle.CLOSED else None,
        dedupe_key=dedupe_key,
    )


def _event(
    *,
    root_id: str = "root-1",
    dedupe_key: str | None = None,
    created_at: datetime,
) -> MattermostCockpitAuditEvent:
    return MattermostCockpitAuditEvent(
        root_id=root_id,
        event_type="created",
        actor_user_id="user-watcher",
        created_at=created_at,
        payload={"source": "test"},
        dedupe_key=dedupe_key,
    )


def test_contracts_validate_expected_exact_ids(contracts: MattermostCockpitContracts) -> None:
    assert contracts.validate_team_id("team-123") == "team-123"
    assert contracts.validate_main_channel_id("chan-main") == "chan-main"
    assert contracts.validate_executions_channel_id("chan-exec") == "chan-exec"
    assert contracts.validate_owner_author_id("user-owner") == "user-owner"
    assert contracts.validate_watcher_user_id("user-watcher") == "user-watcher"

    with pytest.raises(ValueError, match="team_id"):
        contracts.validate_team_id("team-other")
    with pytest.raises(ValueError, match="main_channel_id"):
        contracts.validate_main_channel_id("chan-exec")
    with pytest.raises(ValueError, match="executions_channel_id"):
        contracts.validate_executions_channel_id("chan-main")
    with pytest.raises(ValueError, match="owner_author_id"):
        contracts.validate_owner_author_id("user-other")
    with pytest.raises(ValueError, match="watcher_user_id"):
        contracts.validate_watcher_user_id("user-other")


def test_models_require_utc_and_round_trip(utc_now: datetime) -> None:
    task = _task(created_at=utc_now, updated_at=utc_now)
    payload = task.to_record()

    assert payload["created_at"].endswith("Z")
    assert payload["updated_at"].endswith("Z")

    restored = MattermostCockpitTask.from_row(payload)
    assert restored == task
    assert restored.created_at.tzinfo is timezone.utc
    assert restored.updated_at.tzinfo is timezone.utc

    with pytest.raises(ValueError, match="UTC"):
        _task(created_at=datetime(2026, 7, 24, 12, 34, 56), updated_at=utc_now)


def test_store_default_db_path_uses_hermes_home() -> None:
    assert MattermostCockpitStore.default_db_path() == get_hermes_home() / "mattermost-cockpit" / "state.db"


def test_store_connects_with_wal_foreign_keys_and_busy_timeout(store: MattermostCockpitStore) -> None:
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_store_persists_task_and_audit_event_with_dedupe(store: MattermostCockpitStore, utc_now: datetime) -> None:
    created = _task(created_at=utc_now, updated_at=utc_now, dedupe_key="task-dedupe")
    saved = store.create_task(created)

    assert saved == created
    assert store.get_task("root-1") == created

    deduped = store.create_task(
        replace(
            created,
            root_id="root-2",
            created_at=utc_now.replace(minute=35),
            updated_at=utc_now.replace(minute=35),
        )
    )
    assert deduped.root_id == "root-1"
    assert store.get_task("root-2") is None

    event = _event(created_at=utc_now, dedupe_key="event-dedupe")
    first = store.record_audit_event(event)
    second = store.record_audit_event(event)

    assert first == second
    assert store.list_audit_events("root-1") == [event]


def test_store_rejects_resume_of_closed_task(store: MattermostCockpitStore, utc_now: datetime) -> None:
    store.create_task(_task(created_at=utc_now, updated_at=utc_now, lifecycle=Lifecycle.CLOSED))

    with pytest.raises(ValueError, match="closed"):
        store.resume_task("root-1")


def test_store_rejects_duplicate_root_ids(store: MattermostCockpitStore, utc_now: datetime) -> None:
    store.create_task(_task(created_at=utc_now, updated_at=utc_now, dedupe_key=None))

    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(
            _task(
                root_id="root-1",
                created_at=utc_now.replace(minute=35),
                updated_at=utc_now.replace(minute=35),
                dedupe_key=None,
            )
        )


def test_store_enforces_foreign_keys_for_audit_events(store: MattermostCockpitStore, utc_now: datetime) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.record_audit_event(_event(root_id="missing-root", created_at=utc_now))
