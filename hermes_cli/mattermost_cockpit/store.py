from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

from .contracts import (
    LEGAL_TRANSITIONS,
    Lifecycle,
    MattermostCockpitContracts,
    NONTERMINAL_LIFECYCLES,
    TERMINAL_LIFECYCLES,
    validate_mattermost_id,
)
from .models import MattermostCockpitAuditEvent, MattermostCockpitTask, _require_text, _require_utc, utc_now

DEFAULT_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 1

TASK_COLUMNS = (
    "task_id, team_id, source_channel_id, source_root_id, source_post_id, owner_author_id, "
    "executions_channel_id, execution_root_id, execution_permalink, watcher_user_id, title, lifecycle, "
    "created_at, updated_at, closed_at, source_cursor_ms, execution_cursor_ms, watcher_owner, "
    "watcher_heartbeat_at, result_summary, evidence_json, last_error, dedupe_key, version"
)
TASK_SELECT_SQL = f"SELECT {TASK_COLUMNS} FROM cockpit_tasks WHERE task_id = ?"
TASK_INSERT_SQL = f"INSERT INTO cockpit_tasks ({TASK_COLUMNS}) VALUES ({', '.join(['?'] * 24)})"
TASK_UPDATE_SQL = (
    "UPDATE cockpit_tasks SET "
    "team_id = ?, source_channel_id = ?, source_root_id = ?, source_post_id = ?, owner_author_id = ?, "
    "executions_channel_id = ?, execution_root_id = ?, execution_permalink = ?, watcher_user_id = ?, title = ?, "
    "lifecycle = ?, created_at = ?, updated_at = ?, closed_at = ?, source_cursor_ms = ?, execution_cursor_ms = ?, "
    "watcher_owner = ?, watcher_heartbeat_at = ?, result_summary = ?, evidence_json = ?, last_error = ?, "
    "dedupe_key = ?, version = ? "
    "WHERE task_id = ? AND version = ?"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cockpit_tasks (
    task_id              TEXT PRIMARY KEY,
    team_id              TEXT NOT NULL,
    source_channel_id    TEXT NOT NULL,
    source_root_id       TEXT NOT NULL,
    source_post_id       TEXT NOT NULL,
    owner_author_id      TEXT NOT NULL,
    executions_channel_id TEXT NOT NULL,
    execution_root_id    TEXT,
    execution_permalink  TEXT,
    watcher_user_id      TEXT NOT NULL,
    title                TEXT NOT NULL,
    lifecycle            TEXT NOT NULL CHECK (lifecycle IN ('OPEN', 'RUNNING', 'WAITING_OWNER', 'BLOCKED', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    closed_at            TEXT,
    source_cursor_ms     INTEGER NOT NULL DEFAULT 0 CHECK (source_cursor_ms >= 0),
    execution_cursor_ms  INTEGER NOT NULL DEFAULT 0 CHECK (execution_cursor_ms >= 0),
    watcher_owner        TEXT,
    watcher_heartbeat_at TEXT,
    result_summary       TEXT,
    evidence_json        TEXT NOT NULL DEFAULT '{}',
    last_error           TEXT,
    dedupe_key           TEXT UNIQUE,
    version              INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CHECK (
        (lifecycle IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND closed_at IS NOT NULL)
        OR
        (lifecycle IN ('OPEN', 'RUNNING', 'WAITING_OWNER', 'BLOCKED') AND closed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS cockpit_audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES cockpit_tasks(task_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    actor_user_id   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    dedupe_key      TEXT,
    UNIQUE(task_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_cockpit_tasks_open
    ON cockpit_tasks(lifecycle, created_at, task_id);

CREATE INDEX IF NOT EXISTS idx_cockpit_audit_events_task_created
    ON cockpit_audit_events(task_id, created_at, id);
"""


class MattermostCockpitStore:
    def __init__(
        self,
        *,
        db_path: Path | None = None,
        contracts: MattermostCockpitContracts,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else self.default_db_path()
        self._contracts = contracts
        self._busy_timeout_ms = int(busy_timeout_ms) if int(busy_timeout_ms) > 0 else DEFAULT_BUSY_TIMEOUT_MS

    @staticmethod
    def default_db_path() -> Path:
        return get_hermes_home() / "mattermost-cockpit" / "state.db"

    def _open(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=self._busy_timeout_ms / 1000.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA foreign_keys=ON")
        self._initialize(conn)
        return conn

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextlib.contextmanager
    def connect(self):
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def _validate_task_contracts(self, task: MattermostCockpitTask) -> None:
        self._contracts.validate_team_id(task.team_id)
        if task.source_channel_id != self._contracts.main_channel_id:
            raise ValueError(
                f"source_channel_id must be {self._contracts.main_channel_id!r}; got {task.source_channel_id!r}"
            )
        if task.executions_channel_id != self._contracts.executions_channel_id:
            raise ValueError(
                f"executions_channel_id must be {self._contracts.executions_channel_id!r}; got {task.executions_channel_id!r}"
            )
        self._contracts.validate_owner_author_id(task.owner_author_id)
        self._contracts.validate_watcher_user_id(task.watcher_user_id)

    def _fetch_task(self, conn: sqlite3.Connection, task_id: str) -> MattermostCockpitTask | None:
        row = conn.execute(TASK_SELECT_SQL, (task_id,)).fetchone()
        return MattermostCockpitTask.from_row(row) if row is not None else None

    def _persist_task_update(
        self,
        conn: sqlite3.Connection,
        previous: MattermostCockpitTask,
        updated: MattermostCockpitTask,
    ) -> MattermostCockpitTask:
        record = updated.to_record()
        cursor = conn.execute(
            TASK_UPDATE_SQL,
            (
                record["team_id"],
                record["source_channel_id"],
                record["source_root_id"],
                record["source_post_id"],
                record["owner_author_id"],
                record["executions_channel_id"],
                record["execution_root_id"],
                record["execution_permalink"],
                record["watcher_user_id"],
                record["title"],
                record["lifecycle"],
                record["created_at"],
                record["updated_at"],
                record["closed_at"],
                record["source_cursor_ms"],
                record["execution_cursor_ms"],
                record["watcher_owner"],
                record["watcher_heartbeat_at"],
                record["result_summary"],
                record["evidence_json"],
                record["last_error"],
                record["dedupe_key"],
                record["version"],
                previous.task_id,
                previous.version,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("version mismatch")
        return self.get_task(previous.task_id, conn=conn)  # type: ignore[return-value]

    def create_task(self, task: MattermostCockpitTask) -> MattermostCockpitTask:
        self._validate_task_contracts(task)
        with self.connect() as conn, write_txn(conn):
            if task.dedupe_key is not None:
                existing_row = conn.execute(
                    "SELECT " + TASK_COLUMNS + " FROM cockpit_tasks WHERE dedupe_key = ?",
                    (task.dedupe_key,),
                ).fetchone()
                if existing_row is not None:
                    existing = MattermostCockpitTask.from_row(existing_row)
                    if existing.creation_binding() != task.creation_binding():
                        raise ValueError("dedupe collision: immutable binding differs")
                    return existing
            conn.execute(
                TASK_INSERT_SQL,
                (
                    task.task_id,
                    task.team_id,
                    task.source_channel_id,
                    task.source_root_id,
                    task.source_post_id,
                    task.owner_author_id,
                    task.executions_channel_id,
                    task.execution_root_id,
                    task.execution_permalink,
                    task.watcher_user_id,
                    task.title,
                    task.lifecycle.value,
                    task.to_record()["created_at"],
                    task.to_record()["updated_at"],
                    task.to_record()["closed_at"],
                    task.source_cursor_ms,
                    task.execution_cursor_ms,
                    task.watcher_owner,
                    task.to_record()["watcher_heartbeat_at"],
                    task.result_summary,
                    task.to_record()["evidence_json"],
                    task.last_error,
                    task.dedupe_key,
                    task.version,
                ),
            )
            return self.get_task(task.task_id, conn=conn)  # type: ignore[return-value]

    def get_task(self, task_id: str, *, conn: sqlite3.Connection | None = None) -> MattermostCockpitTask | None:
        owns_conn = conn is None
        active_conn = conn or self._open()
        try:
            return self._fetch_task(active_conn, task_id)
        finally:
            if owns_conn:
                active_conn.close()

    def get(self, task_id: str, *, conn: sqlite3.Connection | None = None) -> MattermostCockpitTask | None:
        return self.get_task(task_id, conn=conn)

    def list_open(self) -> list[MattermostCockpitTask]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT " + TASK_COLUMNS + " FROM cockpit_tasks WHERE lifecycle IN (?, ?, ?, ?) ORDER BY created_at ASC, task_id ASC",
                tuple(state.value for state in NONTERMINAL_LIFECYCLES),
            ).fetchall()
            return [MattermostCockpitTask.from_row(row) for row in rows]

    def bind_execution(self, task_id: str, *, execution_root_id: str, execution_permalink: str) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.lifecycle in TERMINAL_LIFECYCLES:
                raise ValueError("terminal task cannot be bound")
            if current.execution_root_id is not None or current.execution_permalink is not None:
                raise ValueError("execution is already bound")
            updated = replace(
                current,
                execution_root_id=validate_mattermost_id(execution_root_id, "execution_root_id"),
                execution_permalink=_require_text(execution_permalink, "execution_permalink"),
                lifecycle=Lifecycle.RUNNING,
                updated_at=utc_now(),
                version=current.version + 1,
                closed_at=None,
            )
            return self._persist_task_update(conn, current, updated)

    def transition(
        self,
        task_id: str,
        *,
        expected_version: int,
        lifecycle: Lifecycle,
        result_summary: str | None = None,
        evidence: dict | None = None,
        last_error: str | None = None,
        closed_at: datetime | None = None,
    ) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.lifecycle in TERMINAL_LIFECYCLES:
                raise ValueError("terminal task cannot transition")
            allowed = LEGAL_TRANSITIONS[current.lifecycle]
            if lifecycle not in allowed:
                raise ValueError(f"illegal transition from {current.lifecycle.value} to {lifecycle.value}")

            new_closed_at = closed_at
            if lifecycle in TERMINAL_LIFECYCLES:
                new_closed_at = new_closed_at or utc_now()
            elif new_closed_at is not None:
                raise ValueError("nonterminal lifecycle must not carry closed_at")

            if lifecycle is Lifecycle.SUCCEEDED:
                summary = _require_text(result_summary or "", "result_summary")
                new_evidence = dict(evidence or {})
                if not new_evidence:
                    raise ValueError("evidence is required for succeeded tasks")
                new_last_error = _require_text(last_error, "last_error") if last_error is not None else None
            else:
                summary = result_summary if result_summary is not None else current.result_summary
                new_evidence = dict(current.evidence if evidence is None else evidence)
                new_last_error = last_error if last_error is not None else current.last_error

            updated = replace(
                current,
                lifecycle=lifecycle,
                closed_at=new_closed_at,
                result_summary=summary,
                evidence=new_evidence,
                last_error=new_last_error,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def update_cursors(
        self,
        task_id: str,
        *,
        expected_version: int,
        source_cursor_ms: int | None = None,
        execution_cursor_ms: int | None = None,
    ) -> MattermostCockpitTask:
        if source_cursor_ms is None and execution_cursor_ms is None:
            raise ValueError("at least one cursor must be provided")
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            new_source_cursor = current.source_cursor_ms if source_cursor_ms is None else int(source_cursor_ms)
            new_execution_cursor = current.execution_cursor_ms if execution_cursor_ms is None else int(execution_cursor_ms)
            if new_source_cursor < current.source_cursor_ms or new_execution_cursor < current.execution_cursor_ms:
                raise ValueError("cursor updates must be monotonic")
            updated = replace(
                current,
                source_cursor_ms=new_source_cursor,
                execution_cursor_ms=new_execution_cursor,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def claim_watcher(
        self,
        task_id: str,
        *,
        owner: str,
        heartbeat_at: datetime,
        stale_before: datetime,
    ) -> MattermostCockpitTask:
        heartbeat_at = _require_utc(heartbeat_at, "heartbeat_at")
        stale_before = _require_utc(stale_before, "stale_before")
        owner = owner.strip()
        if not owner:
            raise ValueError("owner must not be empty")
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            lease_is_stale = current.watcher_heartbeat_at is None or current.watcher_heartbeat_at < stale_before
            if current.watcher_owner not in (None, owner) and not lease_is_stale:
                raise ValueError("fresh competing lease")
            updated = replace(
                current,
                watcher_owner=owner,
                watcher_heartbeat_at=heartbeat_at,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def heartbeat_watcher(self, task_id: str, *, owner: str, heartbeat_at: datetime) -> MattermostCockpitTask:
        heartbeat_at = _require_utc(heartbeat_at, "heartbeat_at")
        owner = owner.strip()
        if not owner:
            raise ValueError("owner must not be empty")
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.watcher_owner != owner:
                raise ValueError("owner mismatch")
            updated = replace(
                current,
                watcher_heartbeat_at=heartbeat_at,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def release_watcher(self, task_id: str, *, owner: str) -> MattermostCockpitTask:
        owner = owner.strip()
        if not owner:
            raise ValueError("owner must not be empty")
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.watcher_owner != owner:
                raise ValueError("owner mismatch")
            updated = replace(
                current,
                watcher_owner=None,
                watcher_heartbeat_at=None,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def append_audit_event(self, event: MattermostCockpitAuditEvent) -> MattermostCockpitAuditEvent:
        record = event.to_record()
        with self.connect() as conn, write_txn(conn):
            if event.dedupe_key is not None:
                existing = conn.execute(
                    "SELECT task_id, event_type, actor_user_id, created_at, payload_json, dedupe_key "
                    "FROM cockpit_audit_events WHERE task_id = ? AND dedupe_key = ?",
                    (event.task_id, event.dedupe_key),
                ).fetchone()
                if existing is not None:
                    return MattermostCockpitAuditEvent.from_row(existing)
            cursor = conn.execute(
                "INSERT INTO cockpit_audit_events (task_id, event_type, actor_user_id, created_at, payload_json, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record["task_id"],
                    record["event_type"],
                    record["actor_user_id"],
                    record["created_at"],
                    record["payload_json"],
                    record["dedupe_key"],
                ),
            )
            row = conn.execute(
                "SELECT task_id, event_type, actor_user_id, created_at, payload_json, dedupe_key "
                "FROM cockpit_audit_events WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return MattermostCockpitAuditEvent.from_row(row)  # type: ignore[arg-type]

    def record_audit_event(self, event: MattermostCockpitAuditEvent) -> MattermostCockpitAuditEvent:
        return self.append_audit_event(event)

    def list_audit_events(
        self,
        task_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[MattermostCockpitAuditEvent]:
        owns_conn = conn is None
        active_conn = conn or self._open()
        try:
            rows = active_conn.execute(
                "SELECT task_id, event_type, actor_user_id, created_at, payload_json, dedupe_key "
                "FROM cockpit_audit_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
                (task_id,),
            ).fetchall()
            return [MattermostCockpitAuditEvent.from_row(row) for row in rows]
        finally:
            if owns_conn:
                active_conn.close()
