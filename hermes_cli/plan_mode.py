"""Native, cache-stable Plan Mode state and tool policy.

Plan Mode is deliberately enforced at dispatch time.  The agent keeps the
same system prompt and the same tool schemas for the whole conversation, so
entering or leaving the mode never invalidates provider prompt caches.
"""

from __future__ import annotations

import json
import logging
import os
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

_READ_ONLY_TOOL_NAMES = frozenset(
    {
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
    }
)

_SAFE_GIT_SUBCOMMANDS = frozenset(
    {"diff", "log", "show", "rev-parse"}
)
_UNSAFE_GIT_OPTIONS = frozenset(
    {
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
    }
)
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
        raise PlanModeStateUnavailable("Plan Mode state storage could not be loaded.") from exc
    with _DB_LOCK:
        cached = _DB_CACHE.get(home)
        if cached is not None:
            return cached
        try:
            db = SessionDB()
        except Exception as exc:  # pragma: no cover - defensive
            raise PlanModeStateUnavailable("Plan Mode state storage could not be opened.") from exc
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
            parent_raw = parent_row["value"] if hasattr(parent_row, "keys") else parent_row[0]
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
            entered_at=previous.entered_at if previous.active and previous.entered_at else now,
            updated_at=now,
            last_action="entered",
            approval_id="",
        )
        if not _compare_and_set_plan_mode(self.session_id, previous, state):
            raise PlanModeUnavailable("Plan Mode could not be persisted; no planning turn was started.")
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
            raise PlanModeUnavailable("Plan approval could not be persisted; execution remains blocked.")
        return desired

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
            raise PlanModeUnavailable("Build kickoff could not be persisted; execution remains blocked.")
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
            raise PlanModeUnavailable("Plan Mode exit could not be persisted; execution remains blocked.")
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
    return prompt[len(prefix):closing].strip()


def handle_plan_command(session_id: str, args: str = "", *, task_id: str = "") -> PlanCommandResult:
    manager = PlanModeManager(session_id)
    raw = str(args or "").strip()
    lower = raw.lower()

    verb = lower.split(None, 1)[0] if lower else ""
    if verb in {"status", "approve", "exit", "cancel", "reject", "help"} and lower != verb:
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


def migrate_plan_mode_to_session(old_session_id: str, new_session_id: str, *, reason: str = "") -> bool:
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


def _blocked(tool_name: str, args: Mapping[str, Any], code: str, detail: str) -> PlanToolDecision:
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


def _safe_terminal_args(args: Mapping[str, Any], *, task_id: str) -> Optional[Dict[str, Any]]:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if any(char in command for char in _SHELL_CONTROL_CHARS) or "$(" in command or "${" in command:
        return None
    if args.get("background") or args.get("pty") or args.get("notify_on_complete") or args.get("watch_patterns"):
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
            command=shlex.join(
                [
                    *_DYNAMIC_LOADER_RESET,
                    env_path,
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "LANG=C.UTF-8",
                    pwd_path,
                ]
            ),
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
        active = PlanModeManager(session_id).active
    except PlanModeStateUnavailable:
        active = True
        state_unavailable = True
    if not active:
        return PlanToolDecision(True, payload)

    if tool_name in _READ_ONLY_TOOL_NAMES:
        if tool_name == "browser_console":
            if payload.get("expression") is not None or payload.get("clear"):
                return _blocked(tool_name, payload, "browser_console_mutation", "browser console evaluation/clear is not read-only.")
        return PlanToolDecision(True, payload, "read_only")

    if tool_name == "browser_console":
        if payload.get("expression") is None and not payload.get("clear"):
            return PlanToolDecision(True, payload, "read_only")
        return _blocked(tool_name, payload, "browser_console_mutation", "browser console evaluation/clear is not read-only.")

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
            return PlanToolDecision(True, payload, "plan_file_write")
        return _blocked(tool_name, payload, "write_outside_plan_dir", "writes are allowed only below the active workspace's `.hermes/plans` directory.")

    if tool_name == "patch":
        if (
            not state_unavailable
            and payload.get("mode", "replace") == "replace"
            and not payload.get("cross_profile")
            and _plan_file_allowed(payload.get("path"), task_id=task_id)
        ):
            return PlanToolDecision(True, payload, "plan_file_patch")
        return _blocked(tool_name, payload, "patch_outside_plan_dir", "only replace-mode patches inside `.hermes/plans` are allowed.")

    return _blocked(tool_name, payload, "tool_not_read_only", f"tool `{tool_name}` is not on the audited read-only allowlist.")
