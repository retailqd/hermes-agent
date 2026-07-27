from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import time
import dataclasses
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, overload

from .client import MattermostClient
from .contracts import (
    CLEANUP_PENDING_STATE,
    GateDecision,
    Lifecycle,
    MattermostCockpitContracts,
    TERMINAL_LIFECYCLES,
    validate_task_id,
)
from .helpers import HelperBridge
from .models import MattermostCockpitAuditEvent, MattermostCockpitGateRelay, MattermostCockpitTask, utc_now
from .presentation import OwnerDecisionPrompt, render_owner_decision, render_owner_progress
from .relay_renderer import RenderedRelay, render_closed, render_execution_update, render_gate, render_started
from .store import MattermostCockpitStore, WatcherLeaseConflictError

_MAX_SUMMARY_CHARS = 2000
_MAX_EVIDENCE_CHARS = 8000
_UNFOLLOW_READBACK_ATTEMPTS = 10
_UNFOLLOW_READBACK_INTERVAL_SECONDS = 0.5
_UNFOLLOW_STABLE_READS = 5
_RELAY_SCHEMA = 1


class UnitController:
    """Start and stop one systemd user watcher instance per task."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self._run = run

    @staticmethod
    def unit_name(task_id: str) -> str:
        return f"hermes-mattermost-cockpit@{validate_task_id(task_id)}.service"

    def start(self, task_id: str) -> None:
        self._call("start", task_id)

    def stop(self, task_id: str) -> None:
        self._call("stop", task_id)

    def is_active(self, task_id: str) -> bool:
        command = ["systemctl", "--user", "is-active", "--quiet", self.unit_name(task_id)]
        completed = self._run(command, capture_output=True, text=True, timeout=30, check=False)
        return completed.returncode == 0

    def _call(self, action: str, task_id: str) -> None:
        command = ["systemctl", "--user", action, self.unit_name(task_id)]
        completed = self._run(command, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:1000]
            raise RuntimeError(f"systemd {action} failed for cockpit task {task_id}: {stderr or '<empty>'}")


class CockpitService:
    def __init__(
        self,
        *,
        store: MattermostCockpitStore,
        contracts: MattermostCockpitContracts,
        bot_client: MattermostClient,
        owner_client: MattermostClient,
        bridge: HelperBridge,
        units: UnitController,
        base_url: str,
        team_name: str,
        executions_channel_name: str,
        watcher_owner: str | None = None,
        state_dir: Path | None = None,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.contracts = contracts
        self.bot_client = bot_client
        self.owner_client = owner_client
        self.bridge = bridge
        self.units = units
        self.base_url = base_url.rstrip("/")
        self.team_name = team_name.strip()
        self.executions_channel_name = executions_channel_name.strip()
        self.watcher_owner = watcher_owner or f"{socket.gethostname()}:{os.getpid()}"
        self.state_dir = state_dir or (MattermostCockpitStore.default_db_path().parent / "watchers")
        self.now = now
        self.sleep = sleep
        self._watcher_unfollow_initialized: set[str] = set()

    def create(
        self,
        *,
        task_id: str,
        title: str,
        handoff: str,
        source_channel_id: str,
        source_root_id: str,
        source_post_id: str,
        dedupe_key: str,
    ) -> MattermostCockpitTask:
        source_post = self._validate_source_post(
            source_channel_id=source_channel_id,
            source_root_id=source_root_id,
            source_post_id=source_post_id,
        )
        created_at = self.now().astimezone(UTC)
        requested = MattermostCockpitTask(
            task_id=task_id,
            team_id=self.contracts.team_id,
            source_channel_id=source_channel_id,
            source_root_id=source_root_id,
            source_post_id=source_post_id,
            owner_author_id=self.contracts.owner_author_id,
            executions_channel_id=self.contracts.executions_channel_id,
            watcher_user_id=self.contracts.watcher_user_id,
            title=title,
            created_at=created_at,
            updated_at=created_at,
            source_cursor_ms=int(source_post.get("create_at") or 0),
            dedupe_key=dedupe_key,
        )
        task = self.store.create_task(requested)
        if task.lifecycle in TERMINAL_LIFECYCLES or task.cleanup_state == CLEANUP_PENDING_STATE:
            raise ValueError("task cannot be reopened")
        kickoff = self._kickoff_message(task.task_id, task.title, handoff)
        try:
            if task.execution_root_id is None:
                execution_post = self._find_or_create_execution_root(task, kickoff)
                permalink = self._permalink(str(execution_post["id"]))
                task = self.store.attach_execution(
                    task.task_id,
                    expected_version=task.version,
                    execution_root_id=str(execution_post["id"]),
                    execution_permalink=permalink,
                )
            else:
                execution_post = self._validate_execution_post(
                    self.owner_client.get_post(task.execution_root_id),
                    expected_message=kickoff,
                )

            self._ensure_owner_unfollowed(
                user_id=self.contracts.owner_author_id,
                team_id=self.contracts.team_id,
                thread_id=str(execution_post["id"]),
            )
            relay = render_started(task.title, self._permalink(task.execution_root_id))
            self._ensure_source_relay(
                task,
                marker=f"[cockpit-relay:{task.task_id}:started]",
                message=relay.body,
                legacy_relays=(
                    (
                        f"[cockpit-link:{task.task_id}]",
                        f"Execução iniciada: [{task.title}]({task.execution_permalink})",
                    ),
                ),
            )
            is_active = getattr(self.units, "is_active", None)
            if not callable(is_active) or not is_active(task.task_id):
                self.units.start(task.task_id)
            if callable(is_active) and not is_active(task.task_id):
                raise ValueError("watcher unit start readback mismatch")
            current = self._require_task(task.task_id)
            if current.lifecycle in (Lifecycle.OPEN, Lifecycle.BLOCKED):
                task = self.store.mark_running(task.task_id, expected_version=current.version)
            else:
                task = current
            self._audit(task, "execution_started", {"execution_root_id": task.execution_root_id}, f"start:{task.task_id}")
            return self._require_task(task.task_id)
        except Exception as exc:
            current = self._require_task(task.task_id)
            if current.lifecycle not in TERMINAL_LIFECYCLES and current.cleanup_state is None:
                blocked = self.store.record_blocked(
                    task.task_id,
                    expected_version=current.version,
                    last_error=f"{exc.__class__.__name__}: {str(exc)[:800]}",
                )
                try:
                    self._audit(blocked, "execution_start_blocked", {"error_type": exc.__class__.__name__}, f"start-blocked:{blocked.version}")
                except Exception:
                    pass
            raise

    def _ensure_owner_unfollowed(
        self,
        *,
        user_id: str,
        team_id: str,
        thread_id: str,
        force_delete: bool = True,
    ) -> None:
        delete_sent = False
        if force_delete:
            self.owner_client.set_thread_following(
                user_id=user_id,
                team_id=team_id,
                thread_id=thread_id,
                following=False,
            )
            delete_sent = True
        consecutive_unfollowed = 0
        for attempt in range(_UNFOLLOW_READBACK_ATTEMPTS):
            following = self.owner_client.is_thread_following(
                user_id=user_id,
                team_id=team_id,
                thread_id=thread_id,
            )
            if following:
                if not delete_sent or consecutive_unfollowed:
                    self.owner_client.set_thread_following(
                        user_id=user_id,
                        team_id=team_id,
                        thread_id=thread_id,
                        following=False,
                    )
                    delete_sent = True
                consecutive_unfollowed = 0
            else:
                consecutive_unfollowed += 1
                if consecutive_unfollowed >= _UNFOLLOW_STABLE_READS:
                    return
            if attempt + 1 < _UNFOLLOW_READBACK_ATTEMPTS:
                self.sleep(_UNFOLLOW_READBACK_INTERVAL_SECONDS)
        raise ValueError("owner unfollow readback mismatch")

    def open_gate(
        self,
        task_id: str,
        *,
        gate_id: str,
        prompt: str | None = None,
        decision: OwnerDecisionPrompt | None = None,
    ) -> MattermostCockpitTask:
        task = self._require_open_bound_task(task_id)
        if (prompt is None) == (decision is None):
            raise ValueError("provide exactly one of prompt or decision")
        if decision is not None:
            # Structured lay-language contract (T7): the renderer enforces the
            # "Preciso de uma decisão sua" shape, plain language, risk and
            # reply instruction; free-form prompts stay as legacy compat only.
            if decision.technical_url is None:
                decision = dataclasses.replace(
                    decision, technical_url=self._permalink(task.execution_root_id)
                )
            prompt = render_owner_decision(prompt=decision).markdown
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("gate prompt must not be empty")
        marker = f"[cockpit-gate:{task.task_id}:{gate_id}]"
        legacy_prompt_body = f"{marker}\n{prompt}"
        existing_gate = self.store.get_gate(gate_id)
        if (
            existing_gate is not None
            and existing_gate.task_id == task.task_id
            and existing_gate.prompt_body == legacy_prompt_body
        ):
            prompt_body = legacy_prompt_body
            source_message = prompt
        elif decision is not None:
            # Already rendered by the structured contract; render_gate's legacy
            # blocker/decision shape does not apply to it.
            prompt_body = prompt
            source_message = prompt
        else:
            relay = render_gate(prompt, self._permalink(task.execution_root_id))
            prompt_body = relay.body
            source_message = relay.body
        pending_prompt_post_id = self._pending_gate_prompt_id(task.task_id, gate_id)
        with self._gate_lock(task.task_id):
            gate = self.store.get_gate(gate_id)
            if gate is not None:
                if gate.task_id != task.task_id:
                    raise ValueError("gate id collision")
                if gate.prompt_body == legacy_prompt_body:
                    prompt_body = legacy_prompt_body
                    source_message = prompt
                elif gate.prompt_body != prompt_body:
                    raise ValueError("gate id collision")
                if not gate.active:
                    raise ValueError("gate is already resolved")
            else:
                gate = self.store.create_gate(
                    MattermostCockpitGateRelay(
                        gate_id=gate_id,
                        task_id=task.task_id,
                        prompt_post_id=pending_prompt_post_id,
                        prompt_body=prompt_body,
                        created_at=self.now().astimezone(UTC),
                        updated_at=self.now().astimezone(UTC),
                    )
                )
            if gate.prompt_post_id == pending_prompt_post_id:
                post = self._ensure_source_relay(
                    task,
                    marker=marker,
                    message=source_message,
                )
                gate = self.store.bind_gate_prompt(
                    gate_id,
                    task_id=task.task_id,
                    expected_prompt_post_id=pending_prompt_post_id,
                    prompt_post_id=str(post["id"]),
                    prompt_body=prompt_body,
                )
            else:
                self._validate_source_relay_post(
                    self.bot_client.get_post(gate.prompt_post_id),
                    task,
                    marker=marker,
                    message=source_message,
                )
        if not gate.active:
            raise ValueError("gate is already resolved")
        current = self._require_task(task.task_id)
        if current.lifecycle is not Lifecycle.WAITING_OWNER:
            current = self.store.transition(
                task.task_id,
                expected_version=current.version,
                lifecycle=Lifecycle.WAITING_OWNER,
            )
        self._audit(current, "owner_gate_opened", {"gate_id": gate_id, "prompt_post_id": gate.prompt_post_id}, f"gate:{gate_id}")
        return current

    @staticmethod
    def _pending_gate_prompt_id(task_id: str, gate_id: str) -> str:
        digest = hashlib.sha256(f"{task_id}\0{gate_id}".encode()).hexdigest()
        return "0" + digest[:25]

    @contextlib.contextmanager
    def _gate_lock(self, task_id: str):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / f".{validate_task_id(task_id)}.gate.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - cockpit requires POSIX/systemd
                raise RuntimeError("cockpit gate locking requires POSIX") from exc
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def resume_owner_message(
        self,
        task_id: str,
        *,
        gate_id: str,
        decision: GateDecision,
        source_root_id: str,
        source_post_id: str,
        message: str,
    ) -> MattermostCockpitTask:
        task = self._require_open_bound_task(task_id)
        execution_root_id = task.execution_root_id
        if not execution_root_id:
            raise ValueError("execution root is not bound")
        gate = self.store.get_gate(gate_id)
        if gate is None or gate.task_id != task.task_id:
            raise ValueError("active gate mismatch")
        if source_root_id != task.source_root_id:
            raise ValueError("source root mismatch")
        source = self.bot_client.get_post(source_post_id)
        actual_root = str(source.get("root_id") or source.get("id") or "")
        if actual_root != task.source_root_id:
            raise ValueError("source root mismatch")
        if source.get("channel_id") != task.source_channel_id:
            raise ValueError("source channel mismatch")
        if source.get("user_id") != task.owner_author_id:
            raise ValueError("source author mismatch")
        source_body = message.strip()
        if str(source.get("message") or "").strip() != source_body:
            raise ValueError("owner decision body mismatch")
        expected_prompt_marker = f"[cockpit-gate:{task.task_id}:{gate_id}]"
        legacy_prefix = f"{expected_prompt_marker}\n"
        prompt_message = (
            gate.prompt_body[len(legacy_prefix) :]
            if gate.prompt_body.startswith(legacy_prefix)
            else gate.prompt_body
        )
        prompt_post = self._validate_source_relay_post(
            self.bot_client.get_post(gate.prompt_post_id),
            task,
            marker=expected_prompt_marker,
            message=prompt_message,
        )
        if int(source.get("create_at") or 0) < int(prompt_post.get("create_at") or 0):
            raise ValueError("owner decision predates active gate")

        destination_body = f"[cockpit-decision:{gate_id}:{decision.value}]\n{source_body}"
        with self._gate_lock(task.task_id):
            gate = self.store.get_gate(gate_id)
            if gate is None or gate.task_id != task.task_id:
                raise ValueError("active gate mismatch")
            if not gate.active:
                if (
                    gate.decision is not decision
                    or gate.source_owner_post_id != source_post_id
                    or gate.source_body != source_body
                    or gate.destination_body != destination_body
                    or not gate.destination_post_id
                ):
                    raise ValueError("resolved gate replay mismatch")
                self._validate_owner_reply(
                    self.owner_client.get_post(gate.destination_post_id),
                    task,
                    expected_message=destination_body,
                )
                self._ensure_owner_unfollowed(
                    user_id=task.owner_author_id,
                    team_id=task.team_id,
                    thread_id=execution_root_id,
                )
                current = self._require_task(task_id)
                if current.lifecycle is Lifecycle.WAITING_OWNER:
                    current = self.store.mark_running(task_id, expected_version=current.version)
                return current
            current = self._require_open_bound_task(task_id)
            if current.lifecycle is not Lifecycle.WAITING_OWNER:
                raise ValueError("task is not waiting on this gate")
            destination = self._find_owner_reply(task, expected_message=destination_body)
            if destination is None:
                result = self.bridge.post_owner(
                    destination_body,
                    timeout=30,
                    team=self.team_name,
                    channel=self.executions_channel_name,
                    root_id=task.execution_root_id,
                )
                if not result.post_id:
                    raise ValueError("owner helper returned no post id")
                destination = self._validate_owner_reply(
                    self.owner_client.get_post(result.post_id),
                    task,
                    expected_message=destination_body,
                )
            resolved = self.store.resolve_gate(
                gate_id,
                task_id=task.task_id,
                decision=decision,
                source_owner_post_id=source_post_id,
                source_body=source_body,
                destination_post_id=str(destination["id"]),
                destination_body=destination_body,
            )
            self._ensure_owner_unfollowed(
                user_id=task.owner_author_id,
                team_id=task.team_id,
                thread_id=execution_root_id,
            )
        current = self._require_task(task_id)
        if current.lifecycle is Lifecycle.WAITING_OWNER:
            current = self.store.mark_running(task_id, expected_version=current.version)
        self._audit(
            current,
            "owner_decision_relayed",
            {
                "gate_id": gate_id,
                "decision": decision.value,
                "source_post_id": source_post_id,
                "destination_post_id": resolved.destination_post_id,
            },
            f"owner-decision:{gate_id}:{source_post_id}",
        )
        return current

    def relay_status(
        self,
        task_id: str,
        *,
        now_text: str,
        next_milestone: str,
    ) -> dict[str, str]:
        """Upsert the task's single owner-facing status post (T9).

        One status post per task, edited in place: the first call creates the
        post in the source thread (marker in props, never in the body), every
        later call edits the same post. Lay language is enforced by the
        presentation renderer; technical detail belongs in the execution root.
        """
        task = self._require_open_bound_task(task_id)
        rendered = render_owner_progress(now=now_text, next_milestone=next_milestone)
        existing = self.store.get_status_relay(task.task_id)
        if existing is None:
            post = self.bot_client.create_post(
                task.source_channel_id,
                rendered.markdown,
                root_id=task.source_root_id,
                props={
                    "cockpit_relay_marker": f"[cockpit-status:{task.task_id}]",
                    "cockpit_relay_schema": 1,
                },
            )
            record = self.store.upsert_status_relay(
                task.task_id, post_id=str(post["id"]), body=rendered.markdown
            )
        else:
            if existing["body"] != rendered.markdown:
                self.bot_client.update_post(existing["post_id"], rendered.markdown)
            record = self.store.upsert_status_relay(
                task.task_id, post_id=existing["post_id"], body=rendered.markdown
            )
        self._audit(
            task,
            "owner_status_relayed",
            {"post_id": record["post_id"]},
            f"status:{task.task_id}:{hashlib.sha256(rendered.markdown.encode('utf-8')).hexdigest()[:16]}",
        )
        return record

    def close(
        self,
        task_id: str,
        *,
        outcome: Lifecycle,
        summary: str,
        evidence: dict[str, Any],
        last_error: str | None = None,
    ) -> MattermostCockpitTask:
        if outcome not in TERMINAL_LIFECYCLES:
            raise ValueError("close outcome must be terminal")
        summary = summary.strip()
        if not summary:
            raise ValueError("summary must not be empty")
        if len(summary) > _MAX_SUMMARY_CHARS:
            raise ValueError("summary is too large")
        if outcome is Lifecycle.SUCCEEDED and not evidence:
            raise ValueError("evidence is required for succeeded tasks")
        evidence_json = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
        if len(evidence_json) > _MAX_EVIDENCE_CHARS:
            raise ValueError("evidence is too large")
        task = self._require_task(task_id)
        if task.lifecycle in TERMINAL_LIFECYCLES:
            if (
                task.lifecycle is not outcome
                or task.result_summary != summary
                or task.evidence != evidence
                or task.last_error != last_error
            ):
                raise ValueError("terminal close replay mismatch")
            return task
        if not task.execution_root_id:
            raise ValueError("execution root is not bound")
        execution_root_id = task.execution_root_id
        try:
            relay = render_closed(
                outcome=outcome.value,
                requested=task.title,
                summary=summary,
                permalink=self._permalink(execution_root_id),
            )
        except ValueError as exc:
            if "requested work" not in str(exc):
                raise
            # Legacy tasks (autopilot incidents) carry technical titles that the
            # lay-language linter rejects; close must never be impossible, so
            # fall back to a neutral request line. The summary stays strict.
            relay = render_closed(
                outcome=outcome.value,
                requested="o pedido registrado nesta conversa",
                summary=summary,
                permalink=self._permalink(execution_root_id),
            )
        if task.cleanup_state == CLEANUP_PENDING_STATE:
            if task.pending_outcome is not outcome or task.result_summary != summary or task.evidence != evidence:
                raise ValueError("cleanup retry intent mismatch")
        else:
            task = self.store.prepare_close(
                task_id,
                expected_version=task.version,
                outcome=outcome,
                result_summary=summary,
                evidence=evidence,
                last_error=last_error,
            )
        try:
            self._ensure_owner_unfollowed(
                user_id=task.owner_author_id,
                team_id=task.team_id,
                thread_id=execution_root_id,
            )
            self.units.stop(task.task_id)
            is_active = getattr(self.units, "is_active", None)
            if callable(is_active) and is_active(task.task_id):
                raise ValueError("watcher unit stop readback mismatch")

            evidence_digest = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()[:16]
            evidence_marker = f"[cockpit-evidence:{task.task_id}:{evidence_digest}]"
            self._ensure_execution_relay(
                task,
                marker=evidence_marker,
                message=f"{evidence_marker}\n**Evidência final:** `{evidence_json}`",
            )
            marker = f"[cockpit-final:{task.task_id}]"
            self._ensure_source_relay(
                task,
                marker=marker,
                message=relay.body,
            )
        except Exception as exc:
            current = self._require_task(task_id)
            if current.cleanup_state == CLEANUP_PENDING_STATE:
                current = self.store.record_cleanup_error(
                    task_id,
                    expected_version=current.version,
                    last_error=f"{exc.__class__.__name__}: {str(exc)[:800]}",
                )
                try:
                    self._audit(current, "cleanup_failed", {"error_type": exc.__class__.__name__}, f"cleanup-failed:{current.version}")
                except Exception:
                    pass
            raise
        current = self._require_task(task_id)
        closed = self.store.complete_close(
            task_id,
            expected_version=current.version,
            final_last_error=last_error,
        )
        self._audit(closed, "task_closed", {"outcome": outcome.value}, f"close:{task_id}")
        return closed

    def watch_once(self, task_id: str) -> bool:
        task = self._require_task(task_id)
        if task.lifecycle in TERMINAL_LIFECYCLES:
            return False
        if not task.execution_root_id:
            raise ValueError("execution root is not bound")
        now = self.now().astimezone(UTC)
        self.store.claim_watcher(
            task_id,
            owner=self.watcher_owner,
            heartbeat_at=now,
            stale_before=now - timedelta(minutes=3),
            owner_liveness=lambda owner: self._local_watcher_owner_liveness(owner, task_id),
        )
        if task.task_id not in self._watcher_unfollow_initialized:
            self._ensure_owner_unfollowed(
                user_id=task.owner_author_id,
                team_id=task.team_id,
                thread_id=task.execution_root_id,
            )
            self._watcher_unfollow_initialized.add(task.task_id)
        self.bridge.watch_main([task.execution_root_id], timeout=None)
        current = self._require_task(task_id)
        if (
            current.lifecycle in TERMINAL_LIFECYCLES
            or current.cleanup_state == CLEANUP_PENDING_STATE
        ):
            return False
        self._ensure_owner_unfollowed(
            user_id=current.owner_author_id,
            team_id=current.team_id,
            thread_id=task.execution_root_id,
        )
        current = self._require_task(task_id)
        if (
            current.lifecycle in TERMINAL_LIFECYCLES
            or current.cleanup_state == CLEANUP_PENDING_STATE
        ):
            return False
        poll_version = current.version
        state_path = self.state_dir / f"{task_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.bridge.poll_main(
            timeout=30,
            thread_id=task.execution_root_id,
            channel=self.executions_channel_name,
            state=str(state_path),
            max_pages=20,
        )
        thread = self.bot_client.get_thread(task.execution_root_id)
        post_poll_task = self._require_task(task_id)
        if (
            post_poll_task.lifecycle in TERMINAL_LIFECYCLES
            or post_poll_task.cleanup_state == CLEANUP_PENDING_STATE
        ):
            return False
        if post_poll_task.version != poll_version:
            return True
        posts = thread.get("posts") or {}
        new_posts: list[dict[str, Any]] = []
        for post in posts.values():
            if not isinstance(post, dict):
                continue
            if post.get("channel_id") != task.executions_channel_id:
                continue
            if post.get("user_id") != task.owner_author_id:
                continue
            if post.get("root_id") != task.execution_root_id:
                continue
            try:
                create_at = int(post.get("create_at") or 0)
            except (TypeError, ValueError):
                continue
            if create_at > task.execution_cursor_ms:
                new_posts.append(post)
        new_posts.sort(key=lambda post: (int(post.get("create_at") or 0), str(post.get("id") or "")))

        latest: tuple[dict[str, Any], RenderedRelay] | None = None
        permalink = self._permalink(task.execution_root_id)
        for post in new_posts:
            candidate = render_execution_update(str(post.get("message") or ""), permalink)
            if candidate is not None:
                latest = (post, candidate)
        pre_relay_task = self._require_task(task_id)
        if (
            pre_relay_task.lifecycle in TERMINAL_LIFECYCLES
            or pre_relay_task.cleanup_state == CLEANUP_PENDING_STATE
        ):
            return False
        if pre_relay_task.version != poll_version:
            return True
        if latest is not None:
            post, candidate = latest
            marker = f"[cockpit-relay:{task_id}:update:{post['id']}]"
            relay = self._ensure_source_relay(
                task,
                marker=marker,
                message=candidate.body,
                expected_version=poll_version,
            )
            if relay is None:
                current = self._require_task(task_id)
                return not (
                    current.lifecycle in TERMINAL_LIFECYCLES
                    or current.cleanup_state == CLEANUP_PENDING_STATE
                )

        max_cursor = max(
            (int(post.get("create_at") or 0) for post in new_posts),
            default=task.execution_cursor_ms,
        )
        pre_cursor_task = self._require_task(task_id)
        if (
            pre_cursor_task.lifecycle in TERMINAL_LIFECYCLES
            or pre_cursor_task.cleanup_state == CLEANUP_PENDING_STATE
        ):
            return False
        if pre_cursor_task.version != poll_version:
            return True
        if max_cursor > pre_cursor_task.execution_cursor_ms:
            try:
                self.store.update_cursors(
                    task_id,
                    expected_version=poll_version,
                    execution_cursor_ms=max_cursor,
                )
            except ValueError:
                current = self._require_task(task_id)
                if current.version == poll_version:
                    raise
                return not (
                    current.lifecycle in TERMINAL_LIFECYCLES
                    or current.cleanup_state == CLEANUP_PENDING_STATE
                )
        current = self._require_task(task_id)
        if (
            current.lifecycle in TERMINAL_LIFECYCLES
            or current.cleanup_state == CLEANUP_PENDING_STATE
        ):
            return False
        self.store.heartbeat_watcher(
            task_id,
            owner=self.watcher_owner,
            heartbeat_at=self.now().astimezone(UTC),
        )
        return True

    def watch_forever(self, task_id: str) -> str:
        try:
            try:
                while self.watch_once(task_id):
                    pass
            except WatcherLeaseConflictError:
                return "already_running"
            return "stopped"
        finally:
            task = self.store.get_task(task_id)
            if task is not None and task.watcher_owner == self.watcher_owner:
                self.store.release_watcher(task_id, owner=self.watcher_owner)

    def reap_stale(self, *, ttl: timedelta = timedelta(hours=24)) -> dict[str, Any]:
        if ttl.total_seconds() <= 0:
            raise ValueError("reaper ttl must be positive")
        cutoff_ms = int((self.now().astimezone(UTC) - ttl).timestamp() * 1000)
        ttl_hours = ttl.total_seconds() / 3600
        ttl_label = str(int(ttl_hours)) if ttl_hours.is_integer() else f"{ttl_hours:g}"
        result: dict[str, Any] = {
            "closed": [],
            "asked": [],
            "waiting": [],
            "active": [],
            "errors": {},
        }
        for task in self.store.list_open():
            try:
                if task.execution_root_id:
                    posts = (self.bot_client.get_thread(task.execution_root_id).get("posts") or {}).values()
                    activity_ms = max(
                        (
                            max(int(post.get("update_at") or 0), int(post.get("create_at") or 0))
                            for post in posts
                            if isinstance(post, dict)
                            and post.get("channel_id") == task.executions_channel_id
                            and (
                                post.get("id") == task.execution_root_id
                                or post.get("root_id") == task.execution_root_id
                            )
                        ),
                        default=0,
                    )
                else:
                    activity_ms = int(task.updated_at.timestamp() * 1000)
                if activity_ms > cutoff_ms:
                    result["active"].append(task.task_id)
                    continue
                if task.cleanup_state == CLEANUP_PENDING_STATE and task.pending_outcome is not None:
                    self.close(
                        task.task_id,
                        outcome=task.pending_outcome,
                        summary=task.result_summary or "Execução encerrada após concluir a limpeza pendente",
                        evidence=task.evidence,
                        last_error=task.last_error,
                    )
                    result["closed"].append(task.task_id)
                    continue
                if self.store.get_active_gate(task.task_id) is not None:
                    result["waiting"].append(task.task_id)
                    continue
                gate_key = hashlib.sha256(
                    f"{task.task_id}:{cutoff_ms // 86_400_000}".encode()
                ).hexdigest()[:16]
                self.open_gate(
                    task.task_id,
                    gate_id=f"reaper-{gate_key}",
                    decision=OwnerDecisionPrompt(
                        decision="Decidir se a execução parada deve continuar",
                        plain_language=(
                            f"Esta execução está há mais de {ttl_label} horas sem atualização. "
                            "Preciso saber se devo continuar ou encerrar"
                        ),
                        risk=(
                            "Sem resposta, ela permanece pausada e não será "
                            "encerrada automaticamente"
                        ),
                        reply_instruction="responda continuar ou encerrar",
                    ),
                )
                result["asked"].append(task.task_id)
            except Exception as exc:
                result["errors"][task.task_id] = f"{type(exc).__name__}: {exc}"
        return result

    @staticmethod
    def _local_watcher_owner_liveness(owner: str, task_id: str) -> bool | None:
        host, separator, pid_text = owner.rpartition(":")
        if not separator or host != socket.gethostname():
            return None
        try:
            pid = int(pid_text)
            if pid <= 0:
                return None
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (TypeError, ValueError):
            return None
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            return False
        except OSError:
            return None
        argv = [part.decode(errors="replace") for part in command.split(b"\x00") if part]
        module_matches = any(
            argument == "-m"
            and index + 1 < len(argv)
            and argv[index + 1] == "hermes_cli.mattermost_cockpit"
            for index, argument in enumerate(argv)
        )
        executable_matches = bool(argv) and Path(argv[0]).name == "hermes-mattermost-cockpit"
        task_values: list[str] = []
        for index, argument in enumerate(argv):
            if argument == "--task" and index + 1 < len(argv):
                task_values.append(argv[index + 1])
            elif argument.startswith("--task="):
                task_values.append(argument.removeprefix("--task="))
        return (
            (module_matches or executable_matches)
            and "resume" in argv
            and bool(task_values)
            and task_values[-1] == task_id
            and "--watch" in argv
        )

    def status(self, task_id: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        if task_id is not None:
            return self._task_json(self._require_task(task_id))
        return [self._task_json(task) for task in self.store.list_open()]

    def _validate_source_post(self, *, source_channel_id: str, source_root_id: str, source_post_id: str) -> dict[str, Any]:
        self.contracts.validate_main_channel_id(source_channel_id)
        channel = self.bot_client.get_channel(source_channel_id)
        self.contracts.validate_team_id(str(channel.get("team_id") or ""))
        post = self.bot_client.get_post(source_post_id)
        if post.get("channel_id") != source_channel_id:
            raise ValueError("source channel mismatch")
        if post.get("user_id") != self.contracts.owner_author_id:
            raise ValueError("source author mismatch")
        actual_root = str(post.get("root_id") or post.get("id") or "")
        if actual_root != source_root_id:
            raise ValueError("source root mismatch")
        root = self.bot_client.get_post(source_root_id)
        if root.get("channel_id") != source_channel_id or root.get("root_id") not in (None, ""):
            raise ValueError("invalid source root")
        if root.get("user_id") != self.contracts.owner_author_id:
            raise ValueError("source root author mismatch")
        return post

    def _find_or_create_execution_root(self, task: MattermostCockpitTask, kickoff: str) -> dict[str, Any]:
        marker = f"[cockpit-task:{task.task_id}]"
        result = self.owner_client.search_posts(task.team_id, marker)
        posts = list((result.get("posts") or {}).values())
        matching = [post for post in posts if marker in str(post.get("message") or "")]
        if len(matching) > 1:
            raise ValueError("multiple execution roots found for task marker")
        if matching:
            return self._validate_execution_post(matching[0], expected_message=kickoff)
        created = self.bridge.post_owner(
            kickoff,
            timeout=30,
            team=self.team_name,
            channel=self.executions_channel_name,
        )
        if not created.post_id:
            raise ValueError("owner helper returned no post id")
        return self._validate_execution_post(self.owner_client.get_post(created.post_id), expected_message=kickoff)

    def _validate_execution_post(self, post: dict[str, Any], *, expected_message: str) -> dict[str, Any]:
        if post.get("channel_id") != self.contracts.executions_channel_id:
            raise ValueError("execution channel mismatch")
        channel = self.owner_client.get_channel(self.contracts.executions_channel_id)
        self.contracts.validate_team_id(str(channel.get("team_id") or ""))
        if post.get("user_id") != self.contracts.owner_author_id:
            raise ValueError("execution author mismatch")
        if post.get("root_id") not in (None, ""):
            raise ValueError("execution post is not a top-level root")
        if str(post.get("message") or "") != expected_message:
            raise ValueError("execution kickoff mismatch")
        return post

    def _find_owner_reply(
        self,
        task: MattermostCockpitTask,
        *,
        expected_message: str,
    ) -> dict[str, Any] | None:
        if not task.execution_root_id:
            raise ValueError("execution root is not bound")
        thread = self.owner_client.get_thread(task.execution_root_id)
        posts = thread.get("posts") or {}
        matching = [
            post
            for post in posts.values()
            if str(post.get("message") or "") == expected_message
        ]
        if len(matching) > 1:
            raise ValueError("multiple execution owner relays found")
        if not matching:
            return None
        return self._validate_owner_reply(matching[0], task, expected_message=expected_message)

    def _validate_owner_reply(
        self,
        post: dict[str, Any],
        task: MattermostCockpitTask,
        *,
        expected_message: str,
    ) -> dict[str, Any]:
        if post.get("channel_id") != task.executions_channel_id:
            raise ValueError("execution channel mismatch")
        if post.get("user_id") != task.owner_author_id:
            raise ValueError("execution author mismatch")
        if post.get("root_id") != task.execution_root_id:
            raise ValueError("execution root mismatch")
        if str(post.get("message") or "") != expected_message:
            raise ValueError("execution owner relay body mismatch")
        return post

    @staticmethod
    def _source_relay_props(marker: str) -> dict[str, object]:
        return {"cockpit_relay_marker": marker, "cockpit_relay_schema": _RELAY_SCHEMA}

    @staticmethod
    def _post_relay_marker(post: Mapping[str, Any]) -> str | None:
        props = post.get("props")
        if isinstance(props, Mapping):
            value = props.get("cockpit_relay_marker")
            if isinstance(value, str) and value:
                return value
        message = str(post.get("message") or "")
        if not message:
            return None
        first_line = message.splitlines()[0]
        return first_line if first_line.startswith("[cockpit-") else None

    @overload
    def _ensure_source_relay(
        self,
        task: MattermostCockpitTask,
        *,
        marker: str,
        message: str,
        expected_version: None = None,
        legacy_relays: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, Any]: ...

    @overload
    def _ensure_source_relay(
        self,
        task: MattermostCockpitTask,
        *,
        marker: str,
        message: str,
        expected_version: int,
        legacy_relays: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, Any] | None: ...

    def _ensure_source_relay(
        self,
        task: MattermostCockpitTask,
        *,
        marker: str,
        message: str,
        expected_version: int | None = None,
        legacy_relays: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, Any] | None:
        thread = self.bot_client.get_thread(task.source_root_id)
        posts = thread.get("posts") or {}
        relay_contracts = ((marker, message), *legacy_relays)
        markers = {relay_marker for relay_marker, _ in relay_contracts}
        existing = [post for post in posts.values() if self._post_relay_marker(post) in markers]
        if len(existing) > 1:
            raise ValueError("multiple source relays found for marker")
        if existing:
            existing_marker = self._post_relay_marker(existing[0])
            last_error: ValueError | None = None
            for relay_marker, relay_message in relay_contracts:
                if relay_marker != existing_marker:
                    continue
                try:
                    return self._validate_source_relay_post(
                        existing[0], task, marker=relay_marker, message=relay_message
                    )
                except ValueError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise ValueError("relay marker mismatch")
        if expected_version is not None:
            current = self._require_task(task.task_id)
            if (
                current.lifecycle in TERMINAL_LIFECYCLES
                or current.cleanup_state == CLEANUP_PENDING_STATE
                or current.version != expected_version
            ):
                return None
        created = self.bot_client.create_post(
            task.source_channel_id,
            message,
            root_id=task.source_root_id,
            props=self._source_relay_props(marker),
        )
        created_id = str(created.get("id") or "")
        readback = self.bot_client.get_post(created_id)
        validated = self._validate_source_relay_post(
            readback,
            task,
            marker=marker,
            message=message,
        )
        if expected_version is not None:
            current = self._require_task(task.task_id)
            if (
                current.lifecycle in TERMINAL_LIFECYCLES
                or current.cleanup_state == CLEANUP_PENDING_STATE
                or current.version != expected_version
            ):
                self.bot_client.delete_post(created_id)
                posts = self.bot_client.get_thread(task.source_root_id).get("posts") or {}
                lingering = posts.get(created_id)
                if lingering is not None and not int(lingering.get("delete_at") or 0):
                    raise ValueError("late source relay deletion readback failed")
                return None
        return validated

    def _validate_source_relay_post(
        self,
        post: dict[str, Any],
        task: MattermostCockpitTask,
        *,
        marker: str,
        message: str,
    ) -> dict[str, Any]:
        if post.get("channel_id") != task.source_channel_id:
            raise ValueError("relay channel mismatch")
        if post.get("root_id") != task.source_root_id:
            raise ValueError("relay root mismatch")
        if post.get("user_id") != task.watcher_user_id:
            raise ValueError("relay author mismatch")
        body = str(post.get("message") or "")
        props = post.get("props")
        if props is None or (isinstance(props, Mapping) and not props):
            if body != f"{marker}\n{message}":
                raise ValueError("relay body mismatch")
            return post
        if not isinstance(props, Mapping):
            raise ValueError("relay props mismatch")
        schema = props.get("cockpit_relay_schema")
        if (
            props.get("cockpit_relay_marker") != marker
            or type(schema) is not int
            or schema != _RELAY_SCHEMA
        ):
            raise ValueError("relay props mismatch")
        if body != message:
            raise ValueError("relay body mismatch")
        return post

    def _ensure_execution_relay(self, task: MattermostCockpitTask, *, marker: str, message: str) -> dict[str, Any]:
        if not task.execution_root_id:
            raise ValueError("execution root is not bound")
        thread = self.bot_client.get_thread(task.execution_root_id)
        posts = thread.get("posts") or {}
        existing = [post for post in posts.values() if marker in str(post.get("message") or "")]
        if len(existing) > 1:
            raise ValueError("multiple execution relays found for marker")
        if existing:
            return self._validate_execution_bot_reply(existing[0], task, expected_message=message)
        created = self.bot_client.create_post(
            task.executions_channel_id,
            message,
            root_id=task.execution_root_id,
        )
        readback = self.bot_client.get_post(str(created.get("id") or ""))
        return self._validate_execution_bot_reply(readback, task, expected_message=message)

    def _validate_execution_bot_reply(
        self,
        post: dict[str, Any],
        task: MattermostCockpitTask,
        *,
        expected_message: str,
    ) -> dict[str, Any]:
        if post.get("channel_id") != task.executions_channel_id:
            raise ValueError("execution relay channel mismatch")
        if post.get("root_id") != task.execution_root_id:
            raise ValueError("execution relay root mismatch")
        if post.get("user_id") != task.watcher_user_id:
            raise ValueError("execution relay author mismatch")
        if str(post.get("message") or "") != expected_message:
            raise ValueError("execution relay body mismatch")
        return post

    def _validate_bot_reply(
        self,
        post: dict[str, Any],
        task: MattermostCockpitTask,
        *,
        expected_message: str,
    ) -> dict[str, Any]:
        if post.get("channel_id") != task.source_channel_id:
            raise ValueError("relay channel mismatch")
        if post.get("root_id") != task.source_root_id:
            raise ValueError("relay root mismatch")
        if post.get("user_id") != task.watcher_user_id:
            raise ValueError("relay author mismatch")
        if str(post.get("message") or "") != expected_message:
            raise ValueError("relay body mismatch")
        return post

    def _kickoff_message(self, task_id: str, title: str, handoff: str) -> str:
        text = handoff.strip()
        if not text:
            raise ValueError("handoff must not be empty")
        return f"[cockpit-task:{task_id}]\n# {title.strip()}\n\n{text}"

    def _permalink(self, execution_root_id: str | None) -> str:
        if not execution_root_id:
            raise ValueError("execution root is not bound")
        return f"{self.base_url}/{self.team_name}/pl/{execution_root_id}"

    def _require_task(self, task_id: str) -> MattermostCockpitTask:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id!r} does not exist")
        return task

    def _require_open_bound_task(self, task_id: str) -> MattermostCockpitTask:
        task = self._require_task(task_id)
        if task.lifecycle in TERMINAL_LIFECYCLES:
            raise ValueError("terminal task cannot resume")
        if not task.execution_root_id:
            raise ValueError("execution root is not bound")
        return task

    def _audit(self, task: MattermostCockpitTask, event_type: str, payload: dict[str, Any], dedupe_key: str) -> None:
        self.store.append_audit_event(
            MattermostCockpitAuditEvent(
                task_id=task.task_id,
                event_type=event_type,
                actor_user_id=self.contracts.watcher_user_id,
                created_at=self.now().astimezone(UTC),
                payload=payload,
                dedupe_key=dedupe_key,
            )
        )

    @staticmethod
    def _task_json(task: MattermostCockpitTask) -> dict[str, Any]:
        data = asdict(task)
        data["lifecycle"] = task.lifecycle.value
        data["pending_outcome"] = task.pending_outcome.value if task.pending_outcome is not None else None
        for key in ("created_at", "updated_at", "closed_at", "watcher_heartbeat_at"):
            value = data[key]
            data[key] = value.isoformat() if value is not None else None
        return data


__all__ = ["CockpitService", "UnitController"]
