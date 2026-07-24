from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .client import MattermostClient
from .contracts import Lifecycle, MattermostCockpitContracts, TERMINAL_LIFECYCLES, validate_task_id
from .helpers import HelperBridge
from .models import MattermostCockpitAuditEvent, MattermostCockpitTask, utc_now
from .store import MattermostCockpitStore

_MAX_RELAY_CHARS = 3500


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
        kickoff = self._kickoff_message(task.task_id, task.title, handoff)
        if task.execution_root_id is None:
            execution_post = self._find_or_create_execution_root(task, kickoff)
            permalink = f"{self.base_url}/{self.team_name}/pl/{execution_post['id']}"
            task = self.store.bind_execution(
                task.task_id,
                execution_root_id=str(execution_post["id"]),
                execution_permalink=permalink,
            )
        else:
            execution_post = self._validate_execution_post(
                self.owner_client.get_post(task.execution_root_id),
                expected_message=kickoff,
            )

        self.owner_client.set_thread_following(
            user_id=self.contracts.owner_author_id,
            team_id=self.contracts.team_id,
            thread_id=str(execution_post["id"]),
            following=True,
        )
        if not self.owner_client.is_thread_following(
            user_id=self.contracts.owner_author_id,
            team_id=self.contracts.team_id,
            thread_id=str(execution_post["id"]),
        ):
            raise ValueError("owner follow readback mismatch")
        self._ensure_source_relay(
            task,
            marker=f"[cockpit-link:{task.task_id}]",
            message=(
                f"[cockpit-link:{task.task_id}]\n"
                f"Execução iniciada: [{task.title}]({task.execution_permalink})"
            ),
        )
        self.units.start(task.task_id)
        self._audit(task, "execution_started", {"execution_root_id": task.execution_root_id}, f"start:{task.task_id}")
        return self._require_task(task.task_id)

    def resume_owner_message(
        self,
        task_id: str,
        *,
        source_root_id: str,
        source_post_id: str,
        message: str,
    ) -> MattermostCockpitTask:
        task = self._require_open_bound_task(task_id)
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
        if str(source.get("message") or "").strip() != message.strip():
            raise ValueError("owner decision body mismatch")

        dedupe = f"owner-decision:{source_post_id}"
        if any(event.dedupe_key == dedupe for event in self.store.list_audit_events(task_id)):
            return task
        result = self.bridge.post_owner(
            message,
            timeout=30,
            team=self.team_name,
            channel=self.executions_channel_name,
            root_id=task.execution_root_id,
        )
        if not result.post_id:
            raise ValueError("owner helper returned no post id")
        destination = self._validate_owner_reply(self.owner_client.get_post(result.post_id), task)
        self._audit(
            task,
            "owner_decision_relayed",
            {"source_post_id": source_post_id, "destination_post_id": destination["id"]},
            dedupe,
        )
        return self._require_task(task_id)

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
        if outcome is Lifecycle.SUCCEEDED and not evidence:
            raise ValueError("evidence is required for succeeded tasks")
        task = self._require_task(task_id)
        if task.lifecycle in TERMINAL_LIFECYCLES:
            if task.lifecycle is not outcome:
                raise ValueError("terminal outcome mismatch")
            return task
        if not task.execution_root_id:
            raise ValueError("execution root is not bound")

        marker = f"[cockpit-final:{task.task_id}]"
        message = f"{marker}\n**Resultado:** {summary}\n**Evidência:** `{json.dumps(evidence, sort_keys=True, ensure_ascii=False)}`"
        final_post = self._ensure_source_relay(task, marker=marker, message=message)
        self._validate_bot_reply(final_post, task)
        self.owner_client.set_thread_following(
            user_id=task.owner_author_id,
            team_id=task.team_id,
            thread_id=task.execution_root_id,
            following=False,
        )
        if self.owner_client.is_thread_following(
            user_id=task.owner_author_id,
            team_id=task.team_id,
            thread_id=task.execution_root_id,
        ):
            raise ValueError("owner unfollow readback mismatch")
        self.units.stop(task.task_id)
        current = self._require_task(task_id)
        closed = self.store.transition(
            task_id,
            expected_version=current.version,
            lifecycle=outcome,
            result_summary=summary,
            evidence=evidence,
            last_error=last_error,
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
        )
        self.bridge.watch_main([task.execution_root_id], timeout=None)
        current = self._require_task(task_id)
        if current.lifecycle in TERMINAL_LIFECYCLES:
            return False
        state_path = self.state_dir / f"{task_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        poll = self.bridge.poll_main(
            timeout=30,
            thread_id=task.execution_root_id,
            channel=self.executions_channel_name,
            state=str(state_path),
            max_pages=20,
        )
        output = poll.stdout.strip()
        if output and output not in {"NENHUM", "BASELINE"}:
            bounded = output[:_MAX_RELAY_CHARS]
            digest = hashlib.sha256(bounded.encode("utf-8")).hexdigest()[:16]
            marker = f"[cockpit-relay:{task_id}:{digest}]"
            self._ensure_source_relay(task, marker=marker, message=f"{marker}\n{bounded}")
        thread = self.bot_client.get_thread(task.execution_root_id)
        posts = thread.get("posts") or {}
        max_cursor = max((int(post.get("create_at") or 0) for post in posts.values()), default=task.execution_cursor_ms)
        current = self._require_task(task_id)
        if max_cursor > current.execution_cursor_ms:
            self.store.update_cursors(
                task_id,
                expected_version=current.version,
                execution_cursor_ms=max_cursor,
            )
        self.store.heartbeat_watcher(task_id, owner=self.watcher_owner, heartbeat_at=self.now().astimezone(UTC))
        return True

    def watch_forever(self, task_id: str) -> None:
        try:
            while self.watch_once(task_id):
                pass
        finally:
            task = self.store.get_task(task_id)
            if task is not None and task.watcher_owner == self.watcher_owner:
                self.store.release_watcher(task_id, owner=self.watcher_owner)

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

    def _validate_owner_reply(self, post: dict[str, Any], task: MattermostCockpitTask) -> dict[str, Any]:
        if post.get("channel_id") != task.executions_channel_id:
            raise ValueError("execution channel mismatch")
        if post.get("user_id") != task.owner_author_id:
            raise ValueError("execution author mismatch")
        if post.get("root_id") != task.execution_root_id:
            raise ValueError("execution root mismatch")
        return post

    def _ensure_source_relay(self, task: MattermostCockpitTask, *, marker: str, message: str) -> dict[str, Any]:
        thread = self.bot_client.get_thread(task.source_root_id)
        posts = thread.get("posts") or {}
        existing = [post for post in posts.values() if marker in str(post.get("message") or "")]
        if len(existing) > 1:
            raise ValueError("multiple source relays found for marker")
        if existing:
            return self._validate_bot_reply(existing[0], task)
        created = self.bot_client.create_post(task.source_channel_id, message, root_id=task.source_root_id)
        readback = self.bot_client.get_post(str(created.get("id") or ""))
        return self._validate_bot_reply(readback, task)

    def _validate_bot_reply(self, post: dict[str, Any], task: MattermostCockpitTask) -> dict[str, Any]:
        if post.get("channel_id") != task.source_channel_id:
            raise ValueError("relay channel mismatch")
        if post.get("root_id") != task.source_root_id:
            raise ValueError("relay root mismatch")
        if post.get("user_id") != task.watcher_user_id:
            raise ValueError("relay author mismatch")
        return post

    def _kickoff_message(self, task_id: str, title: str, handoff: str) -> str:
        text = handoff.strip()
        if not text:
            raise ValueError("handoff must not be empty")
        return f"[cockpit-task:{task_id}]\n# {title.strip()}\n\n{text}"

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
        for key in ("created_at", "updated_at", "closed_at", "watcher_heartbeat_at"):
            value = data[key]
            data[key] = value.isoformat() if value is not None else None
        return data


__all__ = ["CockpitService", "UnitController"]
