from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

from .contracts import (
    CLEANUP_PENDING_STATE,
    GateDecision,
    LEGAL_TRANSITIONS,
    Lifecycle,
    MattermostCockpitContracts,
    NONTERMINAL_LIFECYCLES,
    TERMINAL_LIFECYCLES,
    validate_mattermost_id,
)
from .models import MattermostCockpitAuditEvent, MattermostCockpitGateRelay, MattermostCockpitTask, _require_text, _require_utc, utc_now

DEFAULT_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 2

TASK_COLUMNS = (
    "task_id, team_id, source_channel_id, source_root_id, source_post_id, owner_author_id, "
    "executions_channel_id, execution_root_id, execution_permalink, watcher_user_id, title, lifecycle, "
    "created_at, updated_at, closed_at, source_cursor_ms, execution_cursor_ms, watcher_owner, "
    "watcher_heartbeat_at, result_summary, evidence_json, last_error, cleanup_state, pending_outcome, dedupe_key, version"
)
TASK_SELECT_SQL = f"SELECT {TASK_COLUMNS} FROM cockpit_tasks WHERE task_id = ?"
TASK_INSERT_SQL = f"INSERT INTO cockpit_tasks ({TASK_COLUMNS}) VALUES ({', '.join(['?'] * 26)})"
TASK_UPDATE_SQL = (
    "UPDATE cockpit_tasks SET "
    "team_id = ?, source_channel_id = ?, source_root_id = ?, source_post_id = ?, owner_author_id = ?, "
    "executions_channel_id = ?, execution_root_id = ?, execution_permalink = ?, watcher_user_id = ?, title = ?, "
    "lifecycle = ?, created_at = ?, updated_at = ?, closed_at = ?, source_cursor_ms = ?, execution_cursor_ms = ?, "
    "watcher_owner = ?, watcher_heartbeat_at = ?, result_summary = ?, evidence_json = ?, last_error = ?, "
    "cleanup_state = ?, pending_outcome = ?, dedupe_key = ?, version = ? "
    "WHERE task_id = ? AND version = ?"
)

GATE_COLUMNS = (
    "gate_id, task_id, prompt_post_id, prompt_body, decision, source_owner_post_id, source_body, "
    "destination_post_id, destination_body, active, created_at, updated_at, last_error"
)
GATE_SELECT_SQL = f"SELECT {GATE_COLUMNS} FROM cockpit_gate_relays WHERE gate_id = ?"
GATE_INSERT_SQL = f"INSERT INTO cockpit_gate_relays ({GATE_COLUMNS}) VALUES ({', '.join(['?'] * 13)})"
GATE_UPDATE_SQL = (
    "UPDATE cockpit_gate_relays SET decision = ?, source_owner_post_id = ?, source_body = ?, "
    "destination_post_id = ?, destination_body = ?, active = ?, updated_at = ?, last_error = ? "
    "WHERE gate_id = ? AND active = 1"
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
    cleanup_state        TEXT,
    pending_outcome      TEXT,
    dedupe_key           TEXT UNIQUE,
    version              INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CHECK (
        (lifecycle IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND closed_at IS NOT NULL)
        OR
        (lifecycle IN ('OPEN', 'RUNNING', 'WAITING_OWNER', 'BLOCKED') AND closed_at IS NULL)
    ),
    CHECK (cleanup_state IS NULL OR cleanup_state = 'cleanup_pending'),
    CHECK ((cleanup_state IS NULL AND pending_outcome IS NULL) OR (cleanup_state = 'cleanup_pending' AND pending_outcome IN ('SUCCEEDED', 'FAILED', 'CANCELLED')))
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

CREATE TABLE IF NOT EXISTS cockpit_gate_relays (
    gate_id              TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL REFERENCES cockpit_tasks(task_id) ON DELETE CASCADE,
    prompt_post_id       TEXT NOT NULL UNIQUE,
    prompt_body          TEXT NOT NULL,
    decision             TEXT CHECK (decision IN ('approve', 'reject', 'clarify')),
    source_owner_post_id TEXT UNIQUE,
    source_body          TEXT,
    destination_post_id  TEXT UNIQUE,
    destination_body     TEXT,
    active               INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    last_error           TEXT,
    CHECK (
        (active = 1 AND decision IS NULL AND source_owner_post_id IS NULL AND destination_post_id IS NULL)
        OR
        (active = 0 AND decision IS NOT NULL AND source_owner_post_id IS NOT NULL AND source_body IS NOT NULL AND destination_post_id IS NOT NULL AND destination_body IS NOT NULL)
    )
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
        self._ensure_column(conn, "cockpit_tasks", "cleanup_state TEXT")
        self._ensure_column(conn, "cockpit_tasks", "pending_outcome TEXT")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_def: str) -> None:
        column_name = column_def.split()[0]
        if column_name not in self._table_columns(conn, table_name):
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")

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
                record["cleanup_state"],
                record["pending_outcome"],
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
            record = task.to_record()
            conn.execute(
                TASK_INSERT_SQL,
                (
                    record["task_id"],
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
                    record["cleanup_state"],
                    record["pending_outcome"],
                    record["dedupe_key"],
                    record["version"],
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

    def attach_execution(
        self,
        task_id: str,
        *,
        expected_version: int,
        execution_root_id: str,
        execution_permalink: str,
    ) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.lifecycle in TERMINAL_LIFECYCLES:
                raise ValueError("terminal task cannot be bound")
            if current.execution_root_id is not None or current.execution_permalink is not None:
                if current.execution_root_id != validate_mattermost_id(execution_root_id, "execution_root_id"):
                    raise ValueError("execution is already bound")
                if current.execution_permalink != _require_text(execution_permalink, "execution_permalink"):
                    raise ValueError("execution is already bound")
                return current
            updated = replace(
                current,
                execution_root_id=validate_mattermost_id(execution_root_id, "execution_root_id"),
                execution_permalink=_require_text(execution_permalink, "execution_permalink"),
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

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

    def mark_running(self, task_id: str, *, expected_version: int) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.lifecycle in TERMINAL_LIFECYCLES:
                raise ValueError("terminal task cannot run")
            if not current.execution_root_id or not current.execution_permalink:
                raise ValueError("execution root is not bound")
            updated = replace(
                current,
                lifecycle=Lifecycle.RUNNING,
                last_error=None,
                cleanup_state=None,
                pending_outcome=None,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def record_blocked(self, task_id: str, *, expected_version: int, last_error: str) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.lifecycle in TERMINAL_LIFECYCLES:
                raise ValueError("terminal task cannot be blocked")
            updated = replace(
                current,
                lifecycle=Lifecycle.BLOCKED,
                last_error=_require_text(last_error, "last_error")[:1000],
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def prepare_close(
        self,
        task_id: str,
        *,
        expected_version: int,
        outcome: Lifecycle,
        result_summary: str,
        evidence: dict,
        last_error: str | None,
    ) -> MattermostCockpitTask:
        if outcome not in TERMINAL_LIFECYCLES:
            raise ValueError("pending outcome must be terminal")
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.lifecycle in TERMINAL_LIFECYCLES:
                raise ValueError("terminal task cannot prepare close")
            summary = _require_text(result_summary, "result_summary")
            new_evidence = dict(evidence or {})
            if outcome is Lifecycle.SUCCEEDED and not new_evidence:
                raise ValueError("evidence is required for succeeded tasks")
            updated = replace(
                current,
                lifecycle=Lifecycle.BLOCKED,
                result_summary=summary,
                evidence=new_evidence,
                last_error=_require_text(last_error, "last_error") if last_error is not None else None,
                cleanup_state=CLEANUP_PENDING_STATE,
                pending_outcome=outcome,
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def record_cleanup_error(self, task_id: str, *, expected_version: int, last_error: str) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.cleanup_state != CLEANUP_PENDING_STATE or current.pending_outcome is None:
                raise ValueError("task is not cleanup pending")
            updated = replace(
                current,
                last_error=_require_text(last_error, "last_error")[:1000],
                updated_at=utc_now(),
                version=current.version + 1,
            )
            return self._persist_task_update(conn, current, updated)

    def complete_close(
        self,
        task_id: str,
        *,
        expected_version: int,
        final_last_error: str | None,
    ) -> MattermostCockpitTask:
        with self.connect() as conn, write_txn(conn):
            current = self._fetch_task(conn, task_id)
            if current is None:
                raise ValueError(f"task {task_id!r} does not exist")
            if current.version != expected_version:
                raise ValueError("version mismatch")
            if current.cleanup_state != CLEANUP_PENDING_STATE or current.pending_outcome is None:
                raise ValueError("task is not cleanup pending")
            updated = replace(
                current,
                lifecycle=current.pending_outcome,
                cleanup_state=None,
                pending_outcome=None,
                last_error=_require_text(final_last_error, "last_error") if final_last_error is not None else None,
                closed_at=utc_now(),
                updated_at=utc_now(),
                version=current.version + 1,
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
        cleanup_state: str | None = None,
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

            new_cleanup_state = cleanup_state if cleanup_state is not None else current.cleanup_state
            if new_cleanup_state is not None and new_cleanup_state != CLEANUP_PENDING_STATE:
                raise ValueError(f"cleanup_state must be {CLEANUP_PENDING_STATE!r}")
            if lifecycle in TERMINAL_LIFECYCLES and new_cleanup_state is not None:
                raise ValueError("terminal lifecycle must not carry cleanup_state")

            updated = replace(
                current,
                lifecycle=lifecycle,
                closed_at=new_closed_at,
                result_summary=summary,
                evidence=new_evidence,
                last_error=new_last_error,
                cleanup_state=new_cleanup_state,
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

    def create_gate(self, gate: MattermostCockpitGateRelay) -> MattermostCockpitGateRelay:
        record = gate.to_record()
        with self.connect() as conn, write_txn(conn):
            existing = conn.execute(GATE_SELECT_SQL, (gate.gate_id,)).fetchone()
            if existing is not None:
                current = MattermostCockpitGateRelay.from_row(existing)
                if (
                    current.task_id != gate.task_id
                    or current.prompt_post_id != gate.prompt_post_id
                    or current.prompt_body != gate.prompt_body
                ):
                    raise ValueError("gate id collision")
                return current
            active = conn.execute(
                "SELECT " + GATE_COLUMNS + " FROM cockpit_gate_relays WHERE task_id = ? AND active = 1",
                (gate.task_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("task already has an active gate")
            conn.execute(
                GATE_INSERT_SQL,
                (
                    record["gate_id"],
                    record["task_id"],
                    record["prompt_post_id"],
                    record["prompt_body"],
                    record["decision"],
                    record["source_owner_post_id"],
                    record["source_body"],
                    record["destination_post_id"],
                    record["destination_body"],
                    record["active"],
                    record["created_at"],
                    record["updated_at"],
                    record["last_error"],
                ),
            )
            row = conn.execute(GATE_SELECT_SQL, (gate.gate_id,)).fetchone()
            return MattermostCockpitGateRelay.from_row(row)  # type: ignore[arg-type]

    def get_gate(self, gate_id: str) -> MattermostCockpitGateRelay | None:
        with self.connect() as conn:
            row = conn.execute(GATE_SELECT_SQL, (gate_id,)).fetchone()
            return MattermostCockpitGateRelay.from_row(row) if row is not None else None

    def get_active_gate(self, task_id: str) -> MattermostCockpitGateRelay | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT " + GATE_COLUMNS + " FROM cockpit_gate_relays WHERE task_id = ? AND active = 1",
                (task_id,),
            ).fetchone()
            return MattermostCockpitGateRelay.from_row(row) if row is not None else None

    def resolve_gate(
        self,
        gate_id: str,
        *,
        task_id: str,
        decision: GateDecision,
        source_owner_post_id: str,
        source_body: str,
        destination_post_id: str,
        destination_body: str,
    ) -> MattermostCockpitGateRelay:
        with self.connect() as conn, write_txn(conn):
            row = conn.execute(GATE_SELECT_SQL, (gate_id,)).fetchone()
            if row is None:
                raise ValueError("gate does not exist")
            current = MattermostCockpitGateRelay.from_row(row)
            expected = (
                task_id,
                decision,
                validate_mattermost_id(source_owner_post_id, "source_owner_post_id"),
                _require_text(source_body, "source_body"),
                validate_mattermost_id(destination_post_id, "destination_post_id"),
                _require_text(destination_body, "destination_body"),
            )
            if not current.active:
                actual = (
                    current.task_id,
                    current.decision,
                    current.source_owner_post_id,
                    current.source_body,
                    current.destination_post_id,
                    current.destination_body,
                )
                if actual != expected:
                    raise ValueError("resolved gate replay mismatch")
                return current
            if current.task_id != task_id:
                raise ValueError("gate task mismatch")
            now = utc_now()
            cursor = conn.execute(
                GATE_UPDATE_SQL,
                (
                    decision.value,
                    expected[2],
                    expected[3],
                    expected[4],
                    expected[5],
                    0,
                    now.isoformat().replace("+00:00", "Z"),
                    None,
                    gate_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("gate resolution race")
            updated = conn.execute(GATE_SELECT_SQL, (gate_id,)).fetchone()
            return MattermostCockpitGateRelay.from_row(updated)  # type: ignore[arg-type]

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
