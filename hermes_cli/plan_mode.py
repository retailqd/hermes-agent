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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

PLAN_STATE_SCHEMA_VERSION = 1
PLAN_MODE_BUILD = "build"
PLAN_MODE_PLAN = "plan"

PLAN_EXECUTION_PROMPT = (
    "[Native Plan Mode approval]\n"
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
    {"status", "diff", "log", "show", "rev-parse", "ls-files"}
)
_UNSAFE_GIT_OPTIONS = frozenset(
    {
        "--config-env",
        "--exec-path",
        "--ext-diff",
        "--no-index",
        "--output",
        "--textconv",
        "-c",
        "-o",
    }
)
_SHELL_CONTROL_CHARS = frozenset(";&|><`\n\r\x00")

_DB_CACHE: Dict[str, Any] = {}
_DB_LOCK = threading.RLock()


class PlanModeUnavailable(RuntimeError):
    """Raised when a requested Plan Mode transition cannot be persisted."""


@dataclass
class PlanModeState:
    """Serializable state stored as ``plan:<session_id>`` in SessionDB."""

    schema_version: int = PLAN_STATE_SCHEMA_VERSION
    mode: str = PLAN_MODE_BUILD
    request: str = ""
    entered_at: float = 0.0
    updated_at: float = 0.0
    last_action: str = ""

    @property
    def active(self) -> bool:
        return self.mode == PLAN_MODE_PLAN

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "PlanModeState":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("plan state must be a JSON object")
        mode = str(data.get("mode") or PLAN_MODE_BUILD).strip().lower()
        if mode not in {PLAN_MODE_BUILD, PLAN_MODE_PLAN}:
            mode = PLAN_MODE_BUILD
        return cls(
            schema_version=int(data.get("schema_version") or PLAN_STATE_SCHEMA_VERSION),
            mode=mode,
            request=str(data.get("request") or ""),
            entered_at=float(data.get("entered_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            last_action=str(data.get("last_action") or ""),
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


def _get_session_db() -> Optional[Any]:
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        home = str(get_hermes_home())
    except Exception as exc:  # pragma: no cover - defensive import path
        logger.debug("PlanMode: SessionDB bootstrap failed: %s", exc)
        return None
    with _DB_LOCK:
        cached = _DB_CACHE.get(home)
        if cached is not None:
            return cached
        try:
            db = SessionDB()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("PlanMode: SessionDB() failed: %s", exc)
            return None
        _DB_CACHE[home] = db
        return db


def load_plan_mode(session_id: str) -> PlanModeState:
    if not session_id:
        return PlanModeState()
    db = _get_session_db()
    if db is None:
        return PlanModeState()
    try:
        raw = db.get_meta(_meta_key(session_id))
    except Exception as exc:
        logger.debug("PlanMode: get_meta failed for %s: %s", session_id, exc)
        return PlanModeState()
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
            logger.debug("PlanMode: parent-state lookup failed for %s: %s", session_id, exc)
        return PlanModeState()
    try:
        return PlanModeState.from_json(raw)
    except Exception as exc:
        logger.warning("PlanMode: invalid state for %s: %s", session_id, exc)
        return PlanModeState()


def save_plan_mode(session_id: str, state: PlanModeState) -> bool:
    if not session_id:
        return False
    db = _get_session_db()
    if db is None:
        return False
    try:
        db.set_meta(_meta_key(session_id), state.to_json())
        stored = db.get_meta(_meta_key(session_id))
        return bool(stored and PlanModeState.from_json(stored).mode == state.mode)
    except Exception as exc:
        logger.warning("PlanMode: could not persist state for %s: %s", session_id, exc)
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
        )
        if not save_plan_mode(self.session_id, state):
            raise PlanModeUnavailable("Plan Mode could not be persisted; no planning turn was started.")
        return state

    def approve(self) -> PlanModeState:
        current = self.state
        if not current.active:
            raise ValueError("No active Plan Mode to approve.")
        current.mode = PLAN_MODE_BUILD
        current.updated_at = time.time()
        current.last_action = "approved"
        if not save_plan_mode(self.session_id, current):
            raise PlanModeUnavailable("Plan approval could not be persisted; execution remains blocked.")
        return current

    def exit(self) -> PlanModeState:
        current = self.state
        if not current.active:
            return current
        current.mode = PLAN_MODE_BUILD
        current.updated_at = time.time()
        current.last_action = "exited"
        if not save_plan_mode(self.session_id, current):
            raise PlanModeUnavailable("Plan Mode exit could not be persisted; execution remains blocked.")
        return current

    def status_line(self) -> str:
        state = self.state
        if state.active:
            suffix = f" Request: {state.request}" if state.request else ""
            return f"PLAN mode is active. Mutating tools are blocked.{suffix}"
        return "BUILD mode is active. Tools follow the normal approval policy."


def build_plan_prompt(request: str, *, task_id: str = "") -> str:
    """Build the self-contained prompt for native runtime Plan Mode.

    ``task_id`` remains accepted for compatibility with existing CLI, gateway,
    TUI, and ACP call sites, but native prompt rendering does not need it.
    """
    from hermes_cli.plan_prompt import render_native_plan_prompt

    return render_native_plan_prompt(request)


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
        manager.approve()
        return PlanCommandResult(
            "approve",
            PLAN_MODE_BUILD,
            "Plan approved. Switching to BUILD and starting execution.",
            prompt=PLAN_EXECUTION_PROMPT,
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
        normalized = dict(args)
        normalized.update(
            command="pwd",
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
        if option in _UNSAFE_GIT_OPTIONS:
            return None

    # Disable pagers and external diff helpers before handing the normalized
    # argv back to the existing terminal implementation.
    subcommand = tokens[1]
    safe_tokens = [
        "git",
        "--no-pager",
        "-c",
        "core.pager=cat",
        "-c",
        "core.externalDiff=",
        subcommand,
    ]
    if subcommand in {"diff", "log", "show"}:
        safe_tokens.append("--no-ext-diff")
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
    if not PlanModeManager(session_id).active:
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
        if not payload.get("cross_profile") and _plan_file_allowed(payload.get("path"), task_id=task_id):
            return PlanToolDecision(True, payload, "plan_file_write")
        return _blocked(tool_name, payload, "write_outside_plan_dir", "writes are allowed only below the active workspace's `.hermes/plans` directory.")

    if tool_name == "patch":
        if (
            payload.get("mode", "replace") == "replace"
            and not payload.get("cross_profile")
            and _plan_file_allowed(payload.get("path"), task_id=task_id)
        ):
            return PlanToolDecision(True, payload, "plan_file_patch")
        return _blocked(tool_name, payload, "patch_outside_plan_dir", "only replace-mode patches inside `.hermes/plans` are allowed.")

    return _blocked(tool_name, payload, "tool_not_read_only", f"tool `{tool_name}` is not on the audited read-only allowlist.")
