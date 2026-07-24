from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

from .contracts import Lifecycle, MattermostCockpitContracts
from .models import MattermostCockpitAuditEvent, MattermostCockpitTask, utc_now

DEFAULT_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cockpit_tasks (
    root_id         TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    owner_author_id TEXT NOT NULL,
    watcher_user_id TEXT NOT NULL,
    lifecycle       TEXT NOT NULL CHECK (lifecycle IN ('OPEN', 'RUNNING', 'BLOCKED', 'CLOSING', 'CLOSED', 'FAILED')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT,
    dedupe_key      TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS cockpit_audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id         TEXT NOT NULL REFERENCES cockpit_tasks(root_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    actor_user_id   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    dedupe_key      TEXT,
    UNIQUE(root_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_cockpit_audit_events_root_created
    ON cockpit_audit_events(root_id, created_at, id);
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
        conn.execute("PRAGMA journal_mode=WAL")
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
        if task.channel_id == self._contracts.main_channel_id:
            self._contracts.validate_main_channel_id(task.channel_id)
        elif task.channel_id == self._contracts.executions_channel_id:
            self._contracts.validate_executions_channel_id(task.channel_id)
        else:
            raise ValueError(
                f"channel_id must be {self._contracts.main_channel_id!r} or {self._contracts.executions_channel_id!r}; got {task.channel_id!r}"
            )
        self._contracts.validate_owner_author_id(task.owner_author_id)
        self._contracts.validate_watcher_user_id(task.watcher_user_id)

    def create_task(self, task: MattermostCockpitTask) -> MattermostCockpitTask:
        self._validate_task_contracts(task)
        record = task.to_record()
        with self.connect() as conn, write_txn(conn):
            if task.dedupe_key is not None:
                existing = conn.execute(
                    "SELECT root_id, team_id, channel_id, owner_author_id, watcher_user_id, lifecycle, created_at, updated_at, closed_at, dedupe_key "
                    "FROM cockpit_tasks WHERE dedupe_key = ?",
                    (task.dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return MattermostCockpitTask.from_row(existing)
            conn.execute(
                "INSERT INTO cockpit_tasks (root_id, team_id, channel_id, owner_author_id, watcher_user_id, lifecycle, created_at, updated_at, closed_at, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.root_id,
                    task.team_id,
                    task.channel_id,
                    task.owner_author_id,
                    task.watcher_user_id,
                    task.lifecycle.value,
                    record["created_at"],
                    record["updated_at"],
                    record["closed_at"],
                    task.dedupe_key,
                ),
            )
            return self.get_task(task.root_id, conn=conn)  # type: ignore[arg-type]

    def get_task(self, root_id: str, *, conn: sqlite3.Connection | None = None) -> MattermostCockpitTask | None:
        owns_conn = conn is None
        active_conn = conn or self._open()
        try:
            row = active_conn.execute(
                "SELECT root_id, team_id, channel_id, owner_author_id, watcher_user_id, lifecycle, created_at, updated_at, closed_at, dedupe_key "
                "FROM cockpit_tasks WHERE root_id = ?",
                (root_id,),
            ).fetchone()
            return MattermostCockpitTask.from_row(row) if row is not None else None
        finally:
            if owns_conn:
                active_conn.close()

    def resume_task(self, root_id: str) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self.get_task(root_id, conn=conn)
            if current is None:
                raise ValueError(f"task {root_id!r} does not exist")
            if current.lifecycle is Lifecycle.CLOSED:
                raise ValueError("closed tasks cannot be resumed")
            resumed = MattermostCockpitTask(
                root_id=current.root_id,
                team_id=current.team_id,
                channel_id=current.channel_id,
                owner_author_id=current.owner_author_id,
                watcher_user_id=current.watcher_user_id,
                lifecycle=Lifecycle.RUNNING,
                created_at=current.created_at,
                updated_at=utc_now(),
                closed_at=None,
                dedupe_key=current.dedupe_key,
            )
            resumed_record = resumed.to_record()
            conn.execute(
                "UPDATE cockpit_tasks SET lifecycle = ?, updated_at = ?, closed_at = NULL WHERE root_id = ?",
                (resumed.lifecycle.value, resumed_record["updated_at"], root_id),
            )
            return resumed

    def record_audit_event(self, event: MattermostCockpitAuditEvent) -> MattermostCockpitAuditEvent:
        record = event.to_record()
        with self.connect() as conn, write_txn(conn):
            if event.dedupe_key is not None:
                existing = conn.execute(
                    "SELECT root_id, event_type, actor_user_id, created_at, payload_json, dedupe_key "
                    "FROM cockpit_audit_events WHERE root_id = ? AND dedupe_key = ?",
                    (event.root_id, event.dedupe_key),
                ).fetchone()
                if existing is not None:
                    return MattermostCockpitAuditEvent.from_row(existing)
            conn.execute(
                "INSERT INTO cockpit_audit_events (root_id, event_type, actor_user_id, created_at, payload_json, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.root_id,
                    event.event_type,
                    event.actor_user_id,
                    record["created_at"],
                    record["payload_json"],
                    event.dedupe_key,
                ),
            )
            rows = self.list_audit_events(event.root_id, conn=conn)
            return rows[-1]

    def list_audit_events(self, root_id: str, *, conn: sqlite3.Connection | None = None) -> list[MattermostCockpitAuditEvent]:
        owns_conn = conn is None
        active_conn = conn or self._open()
        try:
            rows = active_conn.execute(
                "SELECT root_id, event_type, actor_user_id, created_at, payload_json, dedupe_key "
                "FROM cockpit_audit_events WHERE root_id = ? ORDER BY created_at ASC, id ASC",
                (root_id,),
            ).fetchall()
            return [MattermostCockpitAuditEvent.from_row(row) for row in rows]
        finally:
            if owns_conn:
                active_conn.close()
