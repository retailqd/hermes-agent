"""Native, cache-stable Plan Mode state and tool policy.

Plan Mode is deliberately enforced at dispatch time.  The agent keeps the
same system prompt and the same tool schemas for the whole conversation, so
entering or leaving the mode never invalidates provider prompt caches.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shlex
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

PLAN_STATE_SCHEMA_VERSION = 1
PLAN_MODE_BUILD = "build"
PLAN_MODE_PLAN = "plan"

PLAN_EXECUTION_PROMPT_TEMPLATE = (
    "[Native Plan Mode approval:{approval_id}]\n"
    "The user explicitly approved the current plan with `/plan approve`. "
    "Leave planning mode and execute the approved plan now. Reuse the plan "
    "and decisions already present in this conversation, verify the work, "
    "and report the concrete result."
)

_READ_ONLY_TOOL_NAMES = frozenset({
    "browser_get_images",
    "browser_snapshot",
    "browser_vision",
    "clarify",
    "ha_get_state",
    "ha_list_entities",
    "ha_list_services",
    "mcp_filesystem_directory_tree",
    "mcp_filesystem_get_file_info",
    "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes",
    "mcp_filesystem_read_file",
    "mcp_filesystem_read_multiple_files",
    "mcp_filesystem_read_text_file",
    "mcp_filesystem_search_files",
    "lcm_describe",
    "lcm_expand",
    "lcm_grep",
    "read_file",
    "read_terminal",
    "search_files",
    "session_search",
    "skill_view",
    "skills_list",
    "tool_describe",
    "tool_search",
    "vision_analyze",
    "web_extract",
    "web_search",
})

_SAFE_GIT_SUBCOMMANDS = frozenset({"diff", "log", "show", "rev-parse"})
_UNSAFE_GIT_OPTIONS = frozenset({
    "--config-env",
    "--exec-path",
    "--ext-diff",
    "--no-index",
    "--output",
    "--textconv",
    "--show-signature",
    "--show-signatures",
    "--format",
    "--pretty",
    "-c",
    "-o",
})
_SHELL_CONTROL_CHARS = frozenset(";&|><`\n\r\x00")
_TRUSTED_GIT_CANDIDATES = (Path("/usr/bin/git"), Path("/bin/git"))
_TRUSTED_PWD_CANDIDATES = (Path("/usr/bin/pwd"), Path("/bin/pwd"))
_TRUSTED_ENV_CANDIDATES = (Path("/usr/bin/env"), Path("/bin/env"))

_DYNAMIC_LOADER_RESET = (
    "LD_PRELOAD=",
    "LD_AUDIT=",
    "LD_LIBRARY_PATH=",
    "LD_DEBUG=",
    "LD_DEBUG_OUTPUT=",
    "LD_PROFILE=",
    "GCONV_PATH=",
    "LOCPATH=",
    "NLSPATH=",
    "GLIBC_TUNABLES=",
    "MALLOC_TRACE=",
)

_DB_CACHE: Dict[str, Any] = {}
_DB_LOCK = threading.RLock()


class PlanModeUnavailable(RuntimeError):
    """Raised when a requested Plan Mode transition cannot be persisted."""


class PlanModeStateUnavailable(RuntimeError):
    """Raised when persisted mode state cannot be read safely."""


@dataclass
class PlanModeState:
    """Serializable state stored as ``plan:<session_id>`` in SessionDB."""

    schema_version: int = PLAN_STATE_SCHEMA_VERSION
    mode: str = PLAN_MODE_BUILD
    request: str = ""
    entered_at: float = 0.0
    updated_at: float = 0.0
    last_action: str = ""
    approval_id: str = ""
    clarification_count: int = 0
    plan_artifact_count: int = 0
    plan_artifact_path: str = ""
    plan_artifact_sha256: str = ""

    @property
    def active(self) -> bool:
        return self.mode == PLAN_MODE_PLAN

    @property
    def planning(self) -> bool:
        return self.mode == PLAN_MODE_PLAN

    @property
    def build_pending(self) -> bool:
        # Keep approval pending inside the existing PLAN wire value. Older
        # Hermes versions therefore remain fail-closed after a rollback.
        return self.mode == PLAN_MODE_PLAN and bool(self.approval_id)

    @property
    def approved_build(self) -> bool:
        """Whether the exact approved execution turn is currently in flight."""
        return (
            self.mode == PLAN_MODE_BUILD
            and self.last_action == "build_started"
            and bool(self.approval_id)
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "PlanModeState":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("plan state must be a JSON object")
        mode = str(data.get("mode") or PLAN_MODE_BUILD).strip().lower()
        if mode not in {PLAN_MODE_BUILD, PLAN_MODE_PLAN}:
            raise ValueError(f"unsupported plan mode: {mode}")
        schema_version = int(data.get("schema_version") or PLAN_STATE_SCHEMA_VERSION)
        if schema_version != PLAN_STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported plan state schema: {schema_version}")
        return cls(
            schema_version=schema_version,
            mode=mode,
            request=str(data.get("request") or ""),
            entered_at=float(data.get("entered_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            last_action=str(data.get("last_action") or ""),
            approval_id=str(data.get("approval_id") or ""),
            clarification_count=max(0, int(data.get("clarification_count") or 0)),
            plan_artifact_count=max(0, int(data.get("plan_artifact_count") or 0)),
            plan_artifact_path=str(data.get("plan_artifact_path") or ""),
            plan_artifact_sha256=str(data.get("plan_artifact_sha256") or ""),
        )


@dataclass(frozen=True)
class PlanCommandResult:
    action: str
    plan_mode: str
    message: str
    prompt: Optional[str] = None


@dataclass(frozen=True)
class PlanToolDecision:
    allowed: bool
    args: Dict[str, Any]
    code: str = "allow"
    message: str = ""


def _meta_key(session_id: str) -> str:
    return f"plan:{session_id}"


def _get_session_db() -> Any:
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        home = str(get_hermes_home())
    except Exception as exc:  # pragma: no cover - defensive import path
        raise PlanModeStateUnavailable(
            "Plan Mode state storage could not be loaded."
        ) from exc
    with _DB_LOCK:
        cached = _DB_CACHE.get(home)
        if cached is not None:
            return cached
        try:
            from hermes_cli.config import load_config

            sessions_config = load_config().get("sessions") or {}
            # SessionDB's default path is frozen when hermes_state is imported.
            # Pass the runtime-resolved home explicitly so profile switches and
            # hermetic tests cannot open the owner's live state database. Keep
            # the live storage policy too: notably, re-enabling the optional
            # trigram index here can trigger a multi-GB rebuild on first /plan.
            db = SessionDB(
                Path(home) / "state.db",
                fts_trigram_enabled=bool(
                    sessions_config.get("fts_trigram_enabled", True)
                ),
                wal_size_limit_mb=int(sessions_config.get("wal_size_limit_mb", 0) or 0),
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise PlanModeStateUnavailable(
                "Plan Mode state storage could not be opened."
            ) from exc
        _DB_CACHE[home] = db
        return db


def load_plan_mode(session_id: str) -> PlanModeState:
    if not session_id:
        return PlanModeState()
    db = _get_session_db()
    try:
        raw = db.get_meta(_meta_key(session_id))
    except Exception as exc:
        raise PlanModeStateUnavailable(
            f"Plan Mode state could not be read for session {session_id}."
        ) from exc
    if not raw:
        # Compression and branching create parent-linked session ids. If the
        # eager migration write is interrupted, inherit an ACTIVE boundary
        # from the nearest parent instead of silently failing open on BUILD.
        # An explicit state on any parent (including BUILD) stops the walk.
        current = session_id
        seen: set[str] = set()
        try:
            for _ in range(32):
                if not current or current in seen:
                    break
                seen.add(current)
                row = db.get_session(current)
                parent = str((row or {}).get("parent_session_id") or "")
                if not parent:
                    break
                parent_raw = db.get_meta(_meta_key(parent))
                if parent_raw:
                    inherited = PlanModeState.from_json(parent_raw)
                    if inherited.active:
                        inherited.last_action = "inherited"
                        return inherited
                    break
                current = parent
        except Exception as exc:
            raise PlanModeStateUnavailable(
                f"Plan Mode parent state could not be read for session {session_id}."
            ) from exc
        return PlanModeState()
    try:
        return PlanModeState.from_json(raw)
    except Exception as exc:
        logger.warning("PlanMode: invalid state for %s: %s", session_id, exc)
        raise PlanModeStateUnavailable(
            f"Plan Mode state is invalid for session {session_id}."
        ) from exc


def save_plan_mode(session_id: str, state: PlanModeState) -> bool:
    if not session_id:
        return False
    try:
        db = _get_session_db()
        db.set_meta(_meta_key(session_id), state.to_json())
        # SessionDB.set_meta commits atomically or raises. A read-after-write
        # can fail after a successful commit and must not misreport the write
        # as absent, especially for safety-boundary transitions.
        return True
    except Exception as exc:
        logger.warning("PlanMode: could not persist state for %s: %s", session_id, exc)
        return False


def _effective_state_in_transaction(conn: Any, session_id: str) -> PlanModeState:
    """Read the direct or inherited state while holding SessionDB's write lock."""
    row = conn.execute(
        "SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)
    ).fetchone()
    if row is not None:
        raw = row["value"] if hasattr(row, "keys") else row[0]
        return PlanModeState.from_json(raw)

    current = session_id
    seen: set[str] = set()
    for _ in range(32):
        if not current or current in seen:
            break
        seen.add(current)
        session_row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        if session_row is None:
            break
        parent = (
            session_row["parent_session_id"]
            if hasattr(session_row, "keys")
            else session_row[0]
        )
        parent = str(parent or "")
        if not parent:
            break
        parent_row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (_meta_key(parent),)
        ).fetchone()
        if parent_row is not None:
            parent_raw = (
                parent_row["value"] if hasattr(parent_row, "keys") else parent_row[0]
            )
            inherited = PlanModeState.from_json(parent_raw)
            if inherited.active:
                inherited.last_action = "inherited"
                return inherited
            break
        current = parent
    return PlanModeState()


def _compare_and_set_plan_mode(
    session_id: str,
    expected: PlanModeState,
    desired: PlanModeState,
) -> bool:
    """Persist a transition only if the effective state still equals expected."""
    if not session_id:
        return False
    db = _get_session_db()

    def _do(conn: Any) -> bool:
        current = _effective_state_in_transaction(conn, session_id)
        if current.to_json() != expected.to_json():
            return False
        conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_meta_key(session_id), desired.to_json()),
        )
        return True

    try:
        execute_write = getattr(db, "_execute_write", None)
        if callable(execute_write):
            return bool(execute_write(_do))
        # Minimal compatibility path for test doubles. The real SessionDB path
        # above is the atomic contract used by the runtime.
        with _DB_LOCK:
            current = load_plan_mode(session_id)
            if current.to_json() != expected.to_json():
                return False
            db.set_meta(_meta_key(session_id), desired.to_json())
            return True
    except Exception as exc:
        logger.warning("PlanMode: atomic transition failed for %s: %s", session_id, exc)
        return False


class PlanModeManager:
    def __init__(self, session_id: str):
        self.session_id = str(session_id or "").strip()

    @property
    def state(self) -> PlanModeState:
        return load_plan_mode(self.session_id)

    @property
    def active(self) -> bool:
        return self.state.active

    def activate(self, request: str = "") -> PlanModeState:
        now = time.time()
        previous = self.state
        state = PlanModeState(
            mode=PLAN_MODE_PLAN,
            request=str(request or "").strip(),
            entered_at=previous.entered_at
            if previous.active and previous.entered_at
            else now,
            updated_at=now,
            last_action="entered",
            approval_id="",
            clarification_count=0,
            plan_artifact_count=0,
            plan_artifact_path="",
            plan_artifact_sha256="",
        )
        if not _compare_and_set_plan_mode(self.session_id, previous, state):
            raise PlanModeUnavailable(
                "Plan Mode could not be persisted; no planning turn was started."
            )
        return state

    def approve(self) -> PlanModeState:
        current = self.state
        if current.build_pending and current.approval_id:
            return current
        if not current.planning:
            raise ValueError("No active Plan Mode to approve.")
        desired = PlanModeState(**asdict(current))
        desired.updated_at = time.time()
        desired.last_action = "approval_pending"
        desired.approval_id = uuid.uuid4().hex
        if not _compare_and_set_plan_mode(self.session_id, current, desired):
            latest = self.state
            if latest.build_pending and latest.request == current.request:
                return latest
            raise PlanModeUnavailable(
                "Plan approval could not be persisted; execution remains blocked."
            )
        return desired

    def mark_clarified(self) -> PlanModeState:
        """Persist one completed user clarification for the active plan."""
        for _ in range(4):
            current = self.state
            if not current.planning:
                raise ValueError("No active Plan Mode to mark as clarified.")
            desired = PlanModeState(**asdict(current))
            desired.updated_at = time.time()
            desired.last_action = "clarified"
            desired.clarification_count = current.clarification_count + 1
            if _compare_and_set_plan_mode(self.session_id, current, desired):
                return desired
        raise PlanModeUnavailable(
            "Plan clarification could not be persisted; plan saving remains blocked."
        )

    def mark_plan_artifact_saved(
        self,
        path: str,
        content: str = "",
    ) -> PlanModeState:
        """Persist a successful plan-artifact write for completion validation."""
        normalized_path = str(path or "").strip()
        if not normalized_path:
            raise ValueError("Plan artifact path is required.")
        normalized_content = _normalize_plan_text(content)
        content_sha256 = (
            hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
            if normalized_content
            else ""
        )
        for _ in range(4):
            current = self.state
            if not current.planning:
                raise ValueError("No active Plan Mode to mark as saved.")
            desired = PlanModeState(**asdict(current))
            desired.updated_at = time.time()
            desired.last_action = "plan_saved"
            desired.plan_artifact_count = current.plan_artifact_count + 1
            desired.plan_artifact_path = normalized_path
            desired.plan_artifact_sha256 = content_sha256
            if _compare_and_set_plan_mode(self.session_id, current, desired):
                return desired
        raise PlanModeUnavailable(
            "Plan artifact checkpoint could not be persisted; completion remains blocked."
        )

    def begin_build(self, approval_id: str) -> PlanModeState:
        """Atomically release the guard as the exact approved runtime turn starts."""
        current = self.state
        if not current.build_pending:
            raise ValueError("No approved build kickoff is pending.")
        if not approval_id or approval_id != current.approval_id:
            raise ValueError("The build kickoff does not match the current approval.")
        desired = PlanModeState(**asdict(current))
        desired.mode = PLAN_MODE_BUILD
        desired.updated_at = time.time()
        desired.last_action = "build_started"
        # Retain the consumed nonce so a transport retry of the hidden kickoff
        # can be detected and rejected instead of executing the plan twice.
        desired.approval_id = current.approval_id
        if not _compare_and_set_plan_mode(self.session_id, current, desired):
            raise PlanModeUnavailable(
                "Build kickoff could not be persisted; execution remains blocked."
            )
        return desired

    def complete_build(self, approval_id: str) -> PlanModeState:
        """Close the temporary workspace-edit grant after an approved turn."""
        current = self.state
        if not current.approved_build:
            return current
        if not approval_id or approval_id != current.approval_id:
            raise ValueError("The completed build does not match the current approval.")
        desired = PlanModeState(**asdict(current))
        desired.updated_at = time.time()
        desired.last_action = "build_completed"
        desired.approval_id = ""
        if not _compare_and_set_plan_mode(self.session_id, current, desired):
            raise PlanModeUnavailable(
                "Approved build completion could not be persisted; edit approval remains fail-closed."
            )
        return desired

    def exit(self) -> PlanModeState:
        current = self.state
        if not current.active:
            return current
        desired = PlanModeState(**asdict(current))
        desired.mode = PLAN_MODE_BUILD
        desired.updated_at = time.time()
        desired.last_action = "exited"
        desired.approval_id = ""
        if not _compare_and_set_plan_mode(self.session_id, current, desired):
            raise PlanModeUnavailable(
                "Plan Mode exit could not be persisted; execution remains blocked."
            )
        return desired

    def status_line(self) -> str:
        state = self.state
        if state.active:
            suffix = f" Request: {state.request}" if state.request else ""
            if state.build_pending:
                return (
                    "PLAN approval is pending. Mutating tools remain blocked until "
                    f"the exact approved execution turn begins.{suffix}"
                )
            return f"PLAN mode is active. Mutating tools are blocked.{suffix}"
        return "BUILD mode is active. Tools follow the normal approval policy."


def build_plan_prompt(request: str, *, task_id: str = "") -> str:
    """Build the self-contained prompt for native runtime Plan Mode.

    ``task_id`` remains accepted for compatibility with existing CLI, gateway,
    TUI, and ACP call sites, but native prompt rendering does not need it.
    """
    from hermes_cli.plan_prompt import render_native_plan_prompt

    return render_native_plan_prompt(request)


def build_plan_execution_prompt(approval_id: str) -> str:
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        raise ValueError("Plan approval id is required.")
    return PLAN_EXECUTION_PROMPT_TEMPLATE.format(approval_id=approval_id)


def native_plan_execution_approval_id(prompt: Any) -> Optional[str]:
    """Recognize the private approval envelope even when stale or malformed."""
    if not isinstance(prompt, str):
        return None
    prefix = "[Native Plan Mode approval:"
    if not prompt.startswith(prefix):
        return None
    closing = prompt.find("]", len(prefix))
    if closing < 0:
        return ""
    return prompt[len(prefix) : closing].strip()


def handle_plan_command(
    session_id: str, args: str = "", *, task_id: str = ""
) -> PlanCommandResult:
    manager = PlanModeManager(session_id)
    raw = str(args or "").strip()
    lower = raw.lower()

    verb = lower.split(None, 1)[0] if lower else ""
    if (
        verb in {"status", "approve", "exit", "cancel", "reject", "help"}
        and lower != verb
    ):
        return PlanCommandResult(
            "help",
            manager.state.mode,
            f"Unexpected arguments after `{verb}`. Usage: /plan [request] | /plan status | /plan approve | /plan exit",
        )
    if lower == "status" or (not raw and manager.active):
        return PlanCommandResult("status", manager.state.mode, manager.status_line())
    if lower == "approve":
        approved = manager.approve()
        return PlanCommandResult(
            "approve",
            PLAN_MODE_PLAN,
            "Plan approved. Starting execution; mutations stay blocked until the exact approved turn begins.",
            prompt=build_plan_execution_prompt(approved.approval_id),
        )
    if lower in {"exit", "cancel", "reject"}:
        manager.exit()
        return PlanCommandResult(
            "exit",
            PLAN_MODE_BUILD,
            "Plan Mode exited. BUILD mode is active; nothing was executed.",
        )
    if lower == "help":
        return PlanCommandResult(
            "help",
            manager.state.mode,
            "Usage: /plan [request] | /plan status | /plan approve | /plan exit",
        )

    manager.activate(raw)
    try:
        prompt = build_plan_prompt(raw, task_id=task_id or session_id)
    except Exception:
        # Do not strand a session in PLAN when the planning instructions could
        # not be loaded.  A verified state transition is required either way.
        manager.exit()
        raise
    return PlanCommandResult(
        "enter",
        PLAN_MODE_PLAN,
        "PLAN mode activated. Read-only exploration and clarification are allowed.",
        prompt=prompt,
    )


def migrate_plan_mode_to_session(
    old_session_id: str, new_session_id: str, *, reason: str = ""
) -> bool:
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_plan_mode(old_session_id)
        if not state.active or load_plan_mode(new_session_id).active:
            return False
        if not save_plan_mode(new_session_id, state):
            return False
        state.mode = PLAN_MODE_BUILD
        state.updated_at = time.time()
        state.last_action = "migrated"
        save_plan_mode(old_session_id, state)
        logger.debug(
            "PlanMode: migrated %s -> %s (%s)",
            old_session_id,
            new_session_id,
            reason or "rotation",
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("PlanMode: migration failed: %s", exc)
        return False


def _blocked(
    tool_name: str, args: Mapping[str, Any], code: str, detail: str
) -> PlanToolDecision:
    return PlanToolDecision(
        False,
        dict(args or {}),
        code,
        (
            f"Blocked by native Plan Mode: {detail} "
            "Continue planning with read-only tools, or ask the user to run "
            "`/plan approve` before execution."
        ),
    )


_NO_CLARIFICATION_MARKERS = (
    "do not ask questions",
    "don't ask questions",
    "no questions",
    "without questions",
    "não faça perguntas",
    "nao faca perguntas",
    "sem perguntas",
    "use documented defaults",
    "use os padrões",
    "use os padroes",
)


def _plan_requires_clarification(state: PlanModeState) -> bool:
    request = str(state.request or "").strip().casefold()
    return not any(marker in request for marker in _NO_CLARIFICATION_MARKERS)


_PROPOSED_PLAN_BLOCK_RE = re.compile(
    r"\A\s*<proposed_plan>\s*(?P<body>.+?)\s*</proposed_plan>\s*\Z",
    re.IGNORECASE | re.DOTALL,
)

_PLAN_H1_RE = re.compile(r"(?m)^#\s+\S.+$")
_PLAN_SUMMARY_HEADING_RE = re.compile(
    r"(?im)^##\s+(?:resumo(?:\s+executivo)?|executive\s+summary|summary)\s*$"
)
_PLAN_LOCKED_DECISIONS_RE = re.compile(
    r"(?im)^(?:decisões\s+travadas|decisoes\s+travadas|locked\s+decisions|"
    r"key\s+decisions|decisions\s+locked)\s*:\s*$"
)
_PLAN_OUT_OF_SCOPE_RE = re.compile(
    r"(?im)(?:ficam\s+fora\s+(?:da|de)\s+v1|fora\s+do\s+escopo(?:\s+da\s+v1)?|"
    r"out[ -]of[ -]scope(?:\s+for\s+v1)?|excluded\s+from\s+v1)\s*:"
)
_PLAN_IMPLEMENTATION_HEADING_RE = re.compile(
    r"(?im)^##\s+.*(?:plano|implementa(?:ção|cao)|implementation|execu(?:ção|cao)|changes).*$"
)
_PLAN_ACCEPTANCE_HEADING_RE = re.compile(
    r"(?im)^##\s+.*(?:testes?|tests?|aceite|acceptance|validation|verifica(?:ção|cao)).*$"
)
_PLAN_MODEL_ALLOCATION_HEADING_RE = re.compile(
    r"(?im)^##\s+aloca(?:ção|cao)\s+de\s+modelos\s*$"
)
_PLAN_MODEL_ALLOCATION_HEADER_RE = re.compile(
    r"(?im)^\|\s*Trabalho\s*\|\s*Modelo\s*\|\s*Esforço\s*\|\s*Finalidade\s*\|"
    r"\s*Motivo de eficiência\s*\|\s*Gatilho de escalada\s*\|\s*$"
)
_PLAN_MODEL_ALLOCATION_ROW_RE = re.compile(
    r"(?im)^\|(?!\s*[-:]+\s*\|)(?=.*\bgpt-5\.6-sol\b)(?=.*\bxhigh\b).+\|\s*$"
)


def _normalize_plan_text(content: Any) -> str:
    return str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _codex_plan_contract_gaps(body: str) -> list[str]:
    """Return missing pieces from the observed Codex Plan Mode final shape."""
    normalized = _normalize_plan_text(body)
    gaps: list[str] = []
    if not _PLAN_H1_RE.search(normalized):
        gaps.append("a single H1 plan title")
    if not _PLAN_SUMMARY_HEADING_RE.search(normalized):
        gaps.append("a `## Summary`/`## Resumo` executive summary")
    locked = _PLAN_LOCKED_DECISIONS_RE.search(normalized)
    if not locked:
        gaps.append("a `Locked decisions:`/`Decisões travadas:` list")
    else:
        following = normalized[locked.end() :]
        following = following.split("\n## ", 1)[0]
        if not re.search(r"(?m)^\s*[-*]\s+\S", following):
            gaps.append("at least one bullet under locked decisions")
    if not _PLAN_OUT_OF_SCOPE_RE.search(normalized):
        gaps.append("an explicit out-of-scope for v1 statement")
    if not _PLAN_IMPLEMENTATION_HEADING_RE.search(normalized):
        gaps.append("an implementation plan section")
    if not _PLAN_ACCEPTANCE_HEADING_RE.search(normalized):
        gaps.append("a tests and acceptance section")
    if not _PLAN_MODEL_ALLOCATION_HEADING_RE.search(normalized):
        gaps.append("the required `## Alocação de modelos` section")
    elif not _PLAN_MODEL_ALLOCATION_HEADER_RE.search(normalized):
        gaps.append("the exact six-column model-allocation table header")
    elif not _PLAN_MODEL_ALLOCATION_ROW_RE.search(normalized):
        gaps.append("an execution row allocating `gpt-5.6-sol` at `xhigh`")
    return gaps


def build_plan_completion_nudge(
    session_id: str,
    response_text: str,
) -> Optional[str]:
    """Return a bounded-loop nudge when an active PLAN response is incomplete.

    The model remains responsible for planning. This validator only enforces
    observable workflow invariants: a required clarification was resolved, a
    plan artifact was successfully written in this activation, and the final
    answer is exactly one non-empty ``proposed_plan`` block.
    """
    state = PlanModeManager(session_id).state
    if not state.planning or state.build_pending:
        return None

    rendered = str(response_text or "").strip()
    match = _PROPOSED_PLAN_BLOCK_RE.fullmatch(rendered)
    plan_body = _normalize_plan_text(match.group("body")) if match else ""
    has_complete_block = bool(plan_body)

    if _plan_requires_clarification(state) and state.clarification_count < 1:
        if has_complete_block or "<proposed_plan" in rendered.casefold():
            return (
                "[Native Plan Mode completion guard] The required material "
                "clarification has not been resolved. Do not finalize the plan. "
                "Use `clarify` for the smallest material decision and wait for "
                "the user's answer."
            )
        # A normal text response may itself be the required question. Let it
        # reach the user instead of turning Plan Mode into an internal loop.
        return None

    missing = []
    if state.plan_artifact_count < 1 or not state.plan_artifact_path:
        missing.append(
            "save the decision-complete Markdown plan under the active "
            "workspace's `.hermes/plans/` directory"
        )
    if not has_complete_block:
        missing.append(
            "return exactly one non-empty `<proposed_plan>...</proposed_plan>` "
            "block containing that plan"
        )
    elif state.plan_artifact_sha256:
        rendered_sha256 = hashlib.sha256(plan_body.encode("utf-8")).hexdigest()
        if rendered_sha256 != state.plan_artifact_sha256:
            missing.append(
                "make the `<proposed_plan>` body match the saved plan artifact exactly"
            )
    else:
        missing.append(
            "save the final artifact again so its exact content can be verified"
        )
    if has_complete_block:
        missing.extend(_codex_plan_contract_gaps(plan_body))
    if not missing:
        return None

    requirements = "; then ".join(missing)
    return (
        "[Native Plan Mode completion guard] A clarification answer was "
        "resolved, so do not stop at an acknowledgement or scope confirmation. "
        f"Continue the same planning workflow now: {requirements}. Do not ask "
        "the resolved question again and do not implement the plan."
    )


def _clarification_required_block(
    tool_name: str,
    args: Mapping[str, Any],
) -> PlanToolDecision:
    return PlanToolDecision(
        False,
        dict(args or {}),
        "plan_clarification_required",
        (
            "Blocked by native Plan Mode: the first plan artifact cannot be saved "
            "until at least one material user decision has been answered through "
            "`clarify`. Ask the smallest useful structured question now, wait for "
            "the answer, incorporate it, and then retry the plan-file write. This "
            "is not an execution approval; do not ask the user to run `/plan approve`."
        ),
    )


def _workspace_root(task_id: str) -> Optional[Path]:
    try:
        from tools.file_tools import _authoritative_workspace_root

        raw = _authoritative_workspace_root(task_id or "default")
        return Path(raw).expanduser().resolve() if raw else None
    except Exception:
        return None


def _plan_file_allowed(path: Any, *, task_id: str) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    try:
        from tools.file_tools import _resolve_path_for_task

        root = _workspace_root(task_id)
        if root is None:
            return False
        plan_root = (root / ".hermes" / "plans").resolve()
        plan_root.relative_to(root)
        resolved = _resolve_path_for_task(path, task_id or "default")
        if not isinstance(resolved, Path):
            return False
        resolved.resolve().relative_to(plan_root)
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _trusted_executable(candidates: tuple[Path, ...]) -> Optional[str]:
    """Resolve an OS-owned executable without consulting PATH or shell aliases."""
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_file() and os.access(resolved, os.X_OK):
                return str(resolved)
        except OSError:
            continue
    return None


def _safe_terminal_args(
    args: Mapping[str, Any], *, task_id: str
) -> Optional[Dict[str, Any]]:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if (
        any(char in command for char in _SHELL_CONTROL_CHARS)
        or "$(" in command
        or "${" in command
    ):
        return None
    if (
        args.get("background")
        or args.get("pty")
        or args.get("notify_on_complete")
        or args.get("watch_patterns")
    ):
        return None

    workdir = args.get("workdir")
    if workdir:
        root = _workspace_root(task_id)
        try:
            candidate = Path(str(workdir)).expanduser().resolve()
            if root is None:
                return None
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None

    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return None
    if tokens == ["pwd"]:
        pwd_path = _trusted_executable(_TRUSTED_PWD_CANDIDATES)
        env_path = _trusted_executable(_TRUSTED_ENV_CANDIDATES)
        if pwd_path is None or env_path is None:
            return None
        normalized = dict(args)
        normalized.update(
            command=shlex.join([
                *_DYNAMIC_LOADER_RESET,
                env_path,
                "-i",
                "PATH=/usr/bin:/bin",
                "LANG=C.UTF-8",
                pwd_path,
            ]),
            background=False,
            pty=False,
            notify_on_complete=False,
        )
        normalized.pop("watch_patterns", None)
        return normalized
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] not in _SAFE_GIT_SUBCOMMANDS:
        return None
    for token in tokens[2:]:
        option = token.split("=", 1)[0]
        if option in _UNSAFE_GIT_OPTIONS or "%G" in token:
            return None

    git_path = _trusted_executable(_TRUSTED_GIT_CANDIDATES)
    env_path = _trusted_executable(_TRUSTED_ENV_CANDIDATES)
    if git_path is None or env_path is None:
        return None

    # Disable pagers and external diff helpers before handing the normalized
    # argv back to the existing terminal implementation.
    subcommand = tokens[1]
    # A worktree diff runs arbitrary `filter.<driver>.clean` commands selected
    # by repository .gitattributes. Staged diffs compare stored blobs and do
    # not invoke worktree conversion filters.
    if subcommand == "diff" and not any(
        token in {"--cached", "--staged"} for token in tokens[2:]
    ):
        return None
    safe_tokens = [
        # These assignments are applied by the already-running shell before
        # it execs `/usr/bin/env`, so the dynamic loader cannot act on values
        # exported by an earlier BUILD turn. `env -i` then drops every other
        # inherited variable, including all GIT_DIR/WORK_TREE/INDEX redirects.
        *_DYNAMIC_LOADER_RESET,
        env_path,
        "-i",
        "PATH=/usr/bin:/bin",
        "LANG=C.UTF-8",
        "GIT_NO_LAZY_FETCH=1",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        git_path,
        "--no-optional-locks",
        "--no-replace-objects",
        "--no-pager",
        "-c",
        "core.pager=cat",
        "-c",
        "core.externalDiff=",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "log.showSignature=false",
        "-c",
        "gpg.program=/bin/false",
        "-c",
        "gpg.ssh.program=/bin/false",
        subcommand,
    ]
    if subcommand in {"diff", "log", "show"}:
        safe_tokens.append("--no-ext-diff")
        safe_tokens.append("--no-textconv")
    safe_tokens.extend(tokens[2:])
    normalized = dict(args)
    normalized.update(
        command=shlex.join(safe_tokens),
        background=False,
        pty=False,
        notify_on_complete=False,
    )
    normalized.pop("watch_patterns", None)
    return normalized


def evaluate_plan_tool_call(
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    task_id: str = "",
) -> PlanToolDecision:
    """Return the effective arguments or a synthetic block decision."""
    payload = dict(args or {})
    state_unavailable = False
    try:
        state = PlanModeManager(session_id).state
        active = state.active
    except PlanModeStateUnavailable:
        state = PlanModeState(mode=PLAN_MODE_PLAN)
        active = True
        state_unavailable = True
    if not active:
        return PlanToolDecision(True, payload)

    if tool_name == "clarify" and payload.get("choices") is not None:
        return PlanToolDecision(
            False,
            payload,
            "plan_structured_clarify_required",
            (
                "Blocked by native Plan Mode: selectable planning decisions must "
                "use `clarify(questions=[...])`, with one to three structured "
                "questions and consequence descriptions. Retry now with the "
                "structured batch; use legacy `question` only for a genuinely "
                "open-ended answer with no selectable choices."
            ),
        )

    if tool_name in _READ_ONLY_TOOL_NAMES:
        if tool_name == "browser_console":
            if payload.get("expression") is not None or payload.get("clear"):
                return _blocked(
                    tool_name,
                    payload,
                    "browser_console_mutation",
                    "browser console evaluation/clear is not read-only.",
                )
        return PlanToolDecision(True, payload, "read_only")

    if tool_name == "browser_console":
        if payload.get("expression") is None and not payload.get("clear"):
            return PlanToolDecision(True, payload, "read_only")
        return _blocked(
            tool_name,
            payload,
            "browser_console_mutation",
            "browser console evaluation/clear is not read-only.",
        )

    if tool_name == "terminal":
        normalized = _safe_terminal_args(payload, task_id=task_id)
        if normalized is not None:
            return PlanToolDecision(True, normalized, "audited_terminal")
        return _blocked(
            tool_name,
            payload,
            "terminal_not_read_only",
            "terminal is limited to foreground `pwd` and audited git inspection commands without shell composition.",
        )

    if tool_name == "write_file":
        if (
            not state_unavailable
            and not payload.get("cross_profile")
            and _plan_file_allowed(payload.get("path"), task_id=task_id)
        ):
            if _plan_requires_clarification(state) and state.clarification_count < 1:
                return _clarification_required_block(tool_name, payload)
            return PlanToolDecision(True, payload, "plan_file_write")
        return _blocked(
            tool_name,
            payload,
            "write_outside_plan_dir",
            "writes are allowed only below the active workspace's `.hermes/plans` directory.",
        )

    if tool_name == "patch":
        if (
            not state_unavailable
            and payload.get("mode", "replace") == "replace"
            and not payload.get("cross_profile")
            and _plan_file_allowed(payload.get("path"), task_id=task_id)
        ):
            if _plan_requires_clarification(state) and state.clarification_count < 1:
                return _clarification_required_block(tool_name, payload)
            return PlanToolDecision(True, payload, "plan_file_patch")
        return _blocked(
            tool_name,
            payload,
            "patch_outside_plan_dir",
            "only replace-mode patches inside `.hermes/plans` are allowed.",
        )

    return _blocked(
        tool_name,
        payload,
        "tool_not_read_only",
        f"tool `{tool_name}` is not on the audited read-only allowlist.",
    )
