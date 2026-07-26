from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_cli.mattermost_cockpit.contracts import Lifecycle, MattermostCockpitContracts
from hermes_cli.mattermost_cockpit.models import MattermostCockpitAuditEvent, MattermostCockpitTask
from hermes_cli.mattermost_cockpit.store import MattermostCockpitStore, WatcherLeaseConflictError

TEAM_ID = "a" * 26
MAIN_CHANNEL_ID = "b" * 26
EXECUTIONS_CHANNEL_ID = "c" * 26
OWNER_AUTHOR_ID = "d" * 26
WATCHER_USER_ID = "e" * 26
TASK_ID = "f" * 26
SOURCE_ROOT_ID = "g" * 26
SOURCE_POST_ID = "h" * 26
EXECUTION_ROOT_ID = "i" * 26
OTHER_EXECUTION_ROOT_ID = "j" * 26
LEASE_OWNER_1 = "lease-owner-1"
LEASE_OWNER_2 = "lease-owner-2"


@pytest.fixture()
def contracts() -> MattermostCockpitContracts:
    return MattermostCockpitContracts(
        team_id=TEAM_ID,
        main_channel_id=MAIN_CHANNEL_ID,
        executions_channel_id=EXECUTIONS_CHANNEL_ID,
        owner_author_id=OWNER_AUTHOR_ID,
        watcher_user_id=WATCHER_USER_ID,
    )


@pytest.fixture()
def store(tmp_path: Path, contracts: MattermostCockpitContracts) -> MattermostCockpitStore:
    return MattermostCockpitStore(db_path=tmp_path / "state.db", contracts=contracts)


@pytest.fixture()
def utc_now() -> datetime:
    return datetime(2026, 7, 24, 12, 34, 56, tzinfo=UTC)


def _task(
    *,
    task_id: str = TASK_ID,
    lifecycle: Lifecycle = Lifecycle.OPEN,
    dedupe_key: str | None = None,
    evidence: dict | None = None,
    result_summary: str | None = None,
    closed_at: datetime | None = None,
    created_at: datetime,
    updated_at: datetime,
    source_cursor_ms: int = 0,
    execution_cursor_ms: int = 0,
    watcher_owner: str | None = None,
    watcher_heartbeat_at: datetime | None = None,
    source_post_id: str = SOURCE_POST_ID,
    version: int = 1,
) -> MattermostCockpitTask:
    return MattermostCockpitTask(
        task_id=task_id,
        team_id=TEAM_ID,
        source_channel_id=MAIN_CHANNEL_ID,
        source_root_id=SOURCE_ROOT_ID,
        source_post_id=source_post_id,
        owner_author_id=OWNER_AUTHOR_ID,
        executions_channel_id=EXECUTIONS_CHANNEL_ID,
        execution_root_id=None,
        execution_permalink=None,
        watcher_user_id=WATCHER_USER_ID,
        title="Triagem Mattermost",
        lifecycle=lifecycle,
        created_at=created_at,
        updated_at=updated_at,
        closed_at=closed_at,
        source_cursor_ms=source_cursor_ms,
        execution_cursor_ms=execution_cursor_ms,
        watcher_owner=watcher_owner,
        watcher_heartbeat_at=watcher_heartbeat_at,
        result_summary=result_summary,
        evidence=evidence or {},
        last_error=None,
        dedupe_key=dedupe_key,
        version=version,
    )


def _event(
    *,
    task_id: str = TASK_ID,
    dedupe_key: str | None = None,
    created_at: datetime,
) -> MattermostCockpitAuditEvent:
    return MattermostCockpitAuditEvent(
        task_id=task_id,
        event_type="created",
        actor_user_id=WATCHER_USER_ID,
        created_at=created_at,
        payload={"source": "test"},
        dedupe_key=dedupe_key,
    )


def test_contracts_validate_exact_ids_and_reject_invalid_identifiers() -> None:
    contracts = MattermostCockpitContracts(
        team_id=TEAM_ID,
        main_channel_id=MAIN_CHANNEL_ID,
        executions_channel_id=EXECUTIONS_CHANNEL_ID,
        owner_author_id=OWNER_AUTHOR_ID,
        watcher_user_id=WATCHER_USER_ID,
    )

    assert contracts.validate_team_id(TEAM_ID) == TEAM_ID
    assert contracts.validate_main_channel_id(MAIN_CHANNEL_ID) == MAIN_CHANNEL_ID
    assert contracts.validate_executions_channel_id(EXECUTIONS_CHANNEL_ID) == EXECUTIONS_CHANNEL_ID
    assert contracts.validate_owner_author_id(OWNER_AUTHOR_ID) == OWNER_AUTHOR_ID
    assert contracts.validate_watcher_user_id(WATCHER_USER_ID) == WATCHER_USER_ID

    with pytest.raises(ValueError, match="team_id"):
        contracts.validate_team_id("z" * 26)
    with pytest.raises(ValueError, match="main_channel_id"):
        contracts.validate_main_channel_id("z" * 26)
    with pytest.raises(ValueError, match="executions_channel_id"):
        contracts.validate_executions_channel_id("z" * 26)
    with pytest.raises(ValueError, match="owner_author_id"):
        contracts.validate_owner_author_id("z" * 26)
    with pytest.raises(ValueError, match="watcher_user_id"):
        contracts.validate_watcher_user_id("z" * 26)

    with pytest.raises(ValueError, match="26 lowercase alphanumeric"):
        MattermostCockpitContracts(
            team_id="bad-team",
            main_channel_id=MAIN_CHANNEL_ID,
            executions_channel_id=EXECUTIONS_CHANNEL_ID,
            owner_author_id=OWNER_AUTHOR_ID,
            watcher_user_id=WATCHER_USER_ID,
        )


def test_task_round_trips_schema_and_enforces_terminal_invariants(utc_now: datetime) -> None:
    task = _task(created_at=utc_now, updated_at=utc_now)
    row = task.to_record()

    assert row["task_id"] == TASK_ID
    assert row["lifecycle"] == Lifecycle.OPEN.value
    assert row["created_at"].endswith("Z")
    assert row["updated_at"].endswith("Z")
    assert row["version"] == 1

    restored = MattermostCockpitTask.from_row(row)
    assert restored == task
    assert restored.created_at.tzinfo is UTC
    assert restored.updated_at.tzinfo is UTC

    with pytest.raises(ValueError, match="closed_at"):
        _task(
            lifecycle=Lifecycle.FAILED,
            closed_at=None,
            created_at=utc_now,
            updated_at=utc_now,
        )

    with pytest.raises(ValueError, match="nonterminal"):
        _task(
            lifecycle=Lifecycle.OPEN,
            closed_at=utc_now,
            created_at=utc_now,
            updated_at=utc_now,
        )

    with pytest.raises(ValueError, match="result_summary"):
        _task(
            lifecycle=Lifecycle.SUCCEEDED,
            closed_at=utc_now,
            result_summary="",
            evidence={"proof": True},
            created_at=utc_now,
            updated_at=utc_now,
        )

    with pytest.raises(ValueError, match="evidence"):
        _task(
            lifecycle=Lifecycle.SUCCEEDED,
            closed_at=utc_now,
            result_summary="Concluído",
            evidence={},
            created_at=utc_now,
            updated_at=utc_now,
        )


def test_store_connects_with_wal_foreign_keys_busy_timeout_and_quick_check(store: MattermostCockpitStore) -> None:
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

    with store.connect() as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_create_task_requires_expected_channels_and_exact_binding(store: MattermostCockpitStore, utc_now: datetime) -> None:
    saved = store.create_task(_task(created_at=utc_now, updated_at=utc_now, dedupe_key="task-dedupe"))

    assert saved.task_id == TASK_ID
    assert store.get_task(TASK_ID) == saved
    assert store.list_open() == [saved]

    with pytest.raises(ValueError, match="source_channel_id"):
        store.create_task(
            MattermostCockpitTask(
                task_id="k" * 26,
                team_id=TEAM_ID,
                source_channel_id=EXECUTIONS_CHANNEL_ID,
                source_root_id=SOURCE_ROOT_ID,
                source_post_id=SOURCE_POST_ID,
                owner_author_id=OWNER_AUTHOR_ID,
                executions_channel_id=EXECUTIONS_CHANNEL_ID,
                execution_root_id=None,
                execution_permalink=None,
                watcher_user_id=WATCHER_USER_ID,
                title="Triagem Mattermost",
                lifecycle=Lifecycle.OPEN,
                created_at=utc_now,
                updated_at=utc_now,
                evidence={},
                dedupe_key=None,
            )
        )

    with pytest.raises(ValueError, match="executions_channel_id"):
        store.create_task(
            MattermostCockpitTask(
                task_id="l" * 26,
                team_id=TEAM_ID,
                source_channel_id=MAIN_CHANNEL_ID,
                source_root_id=SOURCE_ROOT_ID,
                source_post_id=SOURCE_POST_ID,
                owner_author_id=OWNER_AUTHOR_ID,
                executions_channel_id=MAIN_CHANNEL_ID,
                execution_root_id=None,
                execution_permalink=None,
                watcher_user_id=WATCHER_USER_ID,
                title="Triagem Mattermost",
                lifecycle=Lifecycle.OPEN,
                created_at=utc_now,
                updated_at=utc_now,
                evidence={},
                dedupe_key=None,
            )
        )

    with pytest.raises(ValueError, match="owner_author_id"):
        store.create_task(
            MattermostCockpitTask(
                task_id="m" * 26,
                team_id=TEAM_ID,
                source_channel_id=MAIN_CHANNEL_ID,
                source_root_id=SOURCE_ROOT_ID,
                source_post_id=SOURCE_POST_ID,
                owner_author_id="n" * 26,
                executions_channel_id=EXECUTIONS_CHANNEL_ID,
                execution_root_id=None,
                execution_permalink=None,
                watcher_user_id=WATCHER_USER_ID,
                title="Triagem Mattermost",
                lifecycle=Lifecycle.OPEN,
                created_at=utc_now,
                updated_at=utc_now,
                evidence={},
                dedupe_key=None,
            )
        )

    with pytest.raises(ValueError, match="watcher_user_id"):
        store.create_task(
            MattermostCockpitTask(
                task_id="o" * 26,
                team_id=TEAM_ID,
                source_channel_id=MAIN_CHANNEL_ID,
                source_root_id=SOURCE_ROOT_ID,
                source_post_id=SOURCE_POST_ID,
                owner_author_id=OWNER_AUTHOR_ID,
                executions_channel_id=EXECUTIONS_CHANNEL_ID,
                execution_root_id=None,
                execution_permalink=None,
                watcher_user_id="p" * 26,
                title="Triagem Mattermost",
                lifecycle=Lifecycle.OPEN,
                created_at=utc_now,
                updated_at=utc_now,
                evidence={},
                dedupe_key=None,
            )
        )

    with pytest.raises(ValueError, match="task_id"):
        MattermostCockpitTask(
            task_id="bad TASK",
            team_id=TEAM_ID,
            source_channel_id=MAIN_CHANNEL_ID,
            source_root_id=SOURCE_ROOT_ID,
            source_post_id=SOURCE_POST_ID,
            owner_author_id=OWNER_AUTHOR_ID,
            executions_channel_id=EXECUTIONS_CHANNEL_ID,
            execution_root_id=None,
            execution_permalink=None,
            watcher_user_id=WATCHER_USER_ID,
            title="Triagem Mattermost",
            lifecycle=Lifecycle.OPEN,
            created_at=utc_now,
            updated_at=utc_now,
            evidence={},
            dedupe_key=None,
        )


def test_create_task_is_idempotent_only_for_identical_immutable_binding(store: MattermostCockpitStore, utc_now: datetime) -> None:
    first = store.create_task(_task(created_at=utc_now, updated_at=utc_now, dedupe_key="dedupe-1"))
    second = store.create_task(
        _task(
            task_id="q" * 26,
            created_at=utc_now + timedelta(minutes=1),
            updated_at=utc_now + timedelta(minutes=1),
            dedupe_key="dedupe-1",
        )
    )

    assert second == first

    with pytest.raises(ValueError, match="dedupe"):
        store.create_task(
            _task(
                task_id="r" * 26,
                created_at=utc_now + timedelta(minutes=2),
                updated_at=utc_now + timedelta(minutes=2),
                dedupe_key="dedupe-1",
                source_post_id="s" * 26,
            )
        )


def test_bind_execution_once_and_transition_with_cas_and_evidence_gate(store: MattermostCockpitStore, utc_now: datetime) -> None:
    task = store.create_task(_task(created_at=utc_now, updated_at=utc_now))

    bound = store.bind_execution(task.task_id, execution_root_id=EXECUTION_ROOT_ID, execution_permalink="https://mm.example/p/1")
    assert bound.lifecycle is Lifecycle.RUNNING
    assert bound.execution_root_id == EXECUTION_ROOT_ID
    assert bound.execution_permalink == "https://mm.example/p/1"
    assert bound.version == task.version + 1

    with pytest.raises(ValueError, match="already bound"):
        store.bind_execution(task.task_id, execution_root_id=OTHER_EXECUTION_ROOT_ID, execution_permalink="https://mm.example/p/2")

    waiting = store.transition(bound.task_id, expected_version=bound.version, lifecycle=Lifecycle.WAITING_OWNER)
    assert waiting.lifecycle is Lifecycle.WAITING_OWNER
    assert waiting.version == bound.version + 1

    with pytest.raises(ValueError, match="version"):
        store.transition(waiting.task_id, expected_version=bound.version, lifecycle=Lifecycle.RUNNING)

    with pytest.raises(ValueError, match="legal"):
        store.transition(waiting.task_id, expected_version=waiting.version, lifecycle=Lifecycle.OPEN)

    running = store.transition(waiting.task_id, expected_version=waiting.version, lifecycle=Lifecycle.RUNNING)
    blocked = store.transition(running.task_id, expected_version=running.version, lifecycle=Lifecycle.BLOCKED)
    resumed = store.transition(blocked.task_id, expected_version=blocked.version, lifecycle=Lifecycle.RUNNING)

    with pytest.raises(ValueError, match="evidence"):
        store.transition(
            resumed.task_id,
            expected_version=resumed.version,
            lifecycle=Lifecycle.SUCCEEDED,
            result_summary="Concluído",
            evidence={},
        )

    succeeded = store.transition(
        resumed.task_id,
        expected_version=resumed.version,
        lifecycle=Lifecycle.SUCCEEDED,
        result_summary="Concluído",
        evidence={"proof": ["post-1", "post-2"]},
    )
    assert succeeded.lifecycle is Lifecycle.SUCCEEDED
    assert succeeded.closed_at is not None
    assert succeeded.result_summary == "Concluído"
    assert succeeded.evidence == {"proof": ["post-1", "post-2"]}

    with pytest.raises(ValueError, match="terminal"):
        store.transition(succeeded.task_id, expected_version=succeeded.version, lifecycle=Lifecycle.RUNNING)


def test_update_cursors_is_monotonic_and_uses_cas(store: MattermostCockpitStore, utc_now: datetime) -> None:
    task = store.create_task(_task(created_at=utc_now, updated_at=utc_now))

    advanced = store.update_cursors(task.task_id, expected_version=task.version, source_cursor_ms=100, execution_cursor_ms=10)
    assert advanced.source_cursor_ms == 100
    assert advanced.execution_cursor_ms == 10
    assert advanced.version == task.version + 1

    with pytest.raises(ValueError, match="monotonic"):
        store.update_cursors(advanced.task_id, expected_version=advanced.version, source_cursor_ms=99)

    with pytest.raises(ValueError, match="version"):
        store.update_cursors(advanced.task_id, expected_version=task.version, execution_cursor_ms=11)


def test_claim_heartbeat_release_respects_staleness_and_owner(store: MattermostCockpitStore, utc_now: datetime) -> None:
    task = store.create_task(_task(created_at=utc_now, updated_at=utc_now))

    claimed = store.claim_watcher(task.task_id, owner=LEASE_OWNER_1, heartbeat_at=utc_now, stale_before=utc_now - timedelta(minutes=5))
    assert claimed.watcher_owner == LEASE_OWNER_1
    assert claimed.watcher_heartbeat_at == utc_now

    refreshed = store.claim_watcher(task.task_id, owner=LEASE_OWNER_1, heartbeat_at=utc_now + timedelta(minutes=1), stale_before=utc_now - timedelta(minutes=5))
    assert refreshed.watcher_owner == LEASE_OWNER_1
    assert refreshed.watcher_heartbeat_at == utc_now + timedelta(minutes=1)

    with pytest.raises(WatcherLeaseConflictError, match="fresh competing lease"):
        store.claim_watcher(task.task_id, owner=LEASE_OWNER_2, heartbeat_at=utc_now + timedelta(minutes=2), stale_before=utc_now)

    taken_over = store.claim_watcher(task.task_id, owner=LEASE_OWNER_2, heartbeat_at=utc_now + timedelta(minutes=3), stale_before=utc_now + timedelta(minutes=2))
    assert taken_over.watcher_owner == LEASE_OWNER_2

    heartbeated = store.heartbeat_watcher(task.task_id, owner=LEASE_OWNER_2, heartbeat_at=utc_now + timedelta(minutes=4))
    assert heartbeated.watcher_heartbeat_at == utc_now + timedelta(minutes=4)

    with pytest.raises(ValueError, match="owner"):
        store.heartbeat_watcher(task.task_id, owner=LEASE_OWNER_1, heartbeat_at=utc_now + timedelta(minutes=5))

    released = store.release_watcher(task.task_id, owner=LEASE_OWNER_2)
    assert released.watcher_owner is None
    assert released.watcher_heartbeat_at is None

    with pytest.raises(ValueError, match="owner"):
        store.release_watcher(task.task_id, owner=LEASE_OWNER_1)


def test_claim_watcher_uses_owner_liveness_to_resolve_stale_and_fresh_leases(
    store: MattermostCockpitStore,
    utc_now: datetime,
) -> None:
    task = store.create_task(_task(created_at=utc_now, updated_at=utc_now))
    store.claim_watcher(
        task.task_id,
        owner=LEASE_OWNER_1,
        heartbeat_at=utc_now,
        stale_before=utc_now - timedelta(minutes=5),
    )

    with pytest.raises(WatcherLeaseConflictError, match="fresh competing lease"):
        store.claim_watcher(
            task.task_id,
            owner=LEASE_OWNER_2,
            heartbeat_at=utc_now + timedelta(minutes=10),
            stale_before=utc_now + timedelta(minutes=5),
            owner_liveness=lambda owner: True,
        )

    taken_over = store.claim_watcher(
        task.task_id,
        owner=LEASE_OWNER_2,
        heartbeat_at=utc_now + timedelta(seconds=1),
        stale_before=utc_now - timedelta(minutes=5),
        owner_liveness=lambda owner: False,
    )
    assert taken_over.watcher_owner == LEASE_OWNER_2


def test_audit_events_require_fk_dedupe_restart_persistence_and_quick_check(store: MattermostCockpitStore, utc_now: datetime, tmp_path: Path) -> None:
    task = store.create_task(_task(created_at=utc_now, updated_at=utc_now))

    first = store.append_audit_event(_event(task_id=task.task_id, dedupe_key="audit-1", created_at=utc_now))
    second = store.append_audit_event(_event(task_id=task.task_id, dedupe_key="audit-1", created_at=utc_now + timedelta(seconds=1)))
    assert first == second
    assert store.list_audit_events(task.task_id) == [first]

    with pytest.raises(sqlite3.IntegrityError):
        store.append_audit_event(_event(task_id="r" * 26, created_at=utc_now))

    restarted = MattermostCockpitStore(db_path=tmp_path / "state.db", contracts=store._contracts)
    assert restarted.get_task(task.task_id) == task
    assert restarted.list_audit_events(task.task_id) == [first]

    with restarted.connect() as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
