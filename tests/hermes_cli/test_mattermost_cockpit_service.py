from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.mattermost_cockpit.contracts import GateDecision, Lifecycle, MattermostCockpitContracts
from hermes_cli.mattermost_cockpit.service import CockpitService
from hermes_cli.mattermost_cockpit.store import MattermostCockpitStore

TEAM = "6y5ygu5a9tg47jbyxxffdwp1xw"
MAIN = "1axfo6xfxjg5txcfddmbja8jkh"
EXEC = "c5rhkxsp6t8w9ezuetij5e4gur"
OWNER = "w8t3hdhbkbdafmwcck61xeo69e"
BOT = "3xktxl892li3phr57xs7l43c8w"
SOURCE_ROOT = "a" * 26
SOURCE_POST = "b" * 26
EXEC_ROOT = "c" * 26
DECISION_POST = "d" * 26


class FakeClient:
    def __init__(self, user_id: str, events: list[str]):
        self.user_id = user_id
        self.events = events
        self.posts: dict[str, dict] = {}
        self.channels = {
            MAIN: {"id": MAIN, "team_id": TEAM, "name": "main"},
            EXEC: {"id": EXEC, "team_id": TEAM, "name": "execucoes"},
        }
        self.following_calls: list[bool] = []
        self.following = False
        self.fail_unfollow = False
        self._counter = 0

    def get_post(self, post_id: str) -> dict:
        return self.posts[post_id]

    def get_thread(self, post_id: str) -> dict:
        root = self.posts[post_id]
        posts = {
            pid: post
            for pid, post in self.posts.items()
            if pid == post_id or post.get("root_id") == post_id
        }
        return {"order": list(posts), "posts": posts, "root": root}

    def get_channel(self, channel_id: str) -> dict:
        return self.channels[channel_id]

    def create_post(self, channel_id: str, message: str, *, root_id: str | None = None) -> dict:
        self._counter += 1
        post_id = f"p{self._counter:025d}"
        post = {
            "id": post_id,
            "channel_id": channel_id,
            "user_id": self.user_id,
            "root_id": root_id or "",
            "message": message,
            "create_at": 1000 + self._counter,
        }
        self.posts[post_id] = post
        self.events.append(f"post:{self.user_id}:{channel_id}")
        return post

    def search_posts(self, team_id: str, terms: str) -> dict:
        assert team_id == TEAM
        matches = {pid: p for pid, p in self.posts.items() if terms in p.get("message", "")}
        return {"order": list(matches), "posts": matches}

    def set_thread_following(self, *, user_id: str, team_id: str, thread_id: str, following: bool) -> dict:
        assert user_id == OWNER and team_id == TEAM
        if not following and self.fail_unfollow:
            raise RuntimeError("unfollow failed")
        self.following_calls.append(following)
        self.following = following
        self.events.append(f"follow:{following}")
        return {"status": "OK", "thread_id": thread_id}

    def is_thread_following(self, *, user_id: str, team_id: str, thread_id: str) -> bool:
        assert user_id == OWNER and team_id == TEAM and thread_id
        return self.following


class FakeBridge:
    def __init__(self, owner_client: FakeClient):
        self.owner_client = owner_client
        self.posts: list[tuple[str, str | None]] = []
        self.poll_output = "NENHUM"
        self.poll_calls = 0
        self.watch_calls = 0

    def post_owner(self, message: str, *, timeout: float, team=None, channel=None, root_id=None):
        del timeout, team, channel
        self.posts.append((message, root_id))
        post = self.owner_client.create_post(EXEC, message, root_id=root_id)
        return SimpleNamespace(post_id=post["id"], stdout="", stderr="")

    def watch_main(self, root_ids, *, timeout=None):
        del timeout
        self.watch_calls += 1
        return SimpleNamespace(stdout="WAKE: test", stderr="")

    def poll_main(self, **kwargs):
        del kwargs
        self.poll_calls += 1
        return SimpleNamespace(stdout=self.poll_output, stderr="")


class FakeUnits:
    def __init__(self, events: list[str]):
        self.events = events
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.active: set[str] = set()

    def start(self, task_id: str) -> None:
        self.started.append(task_id)
        self.active.add(task_id)
        self.events.append("unit:start")

    def stop(self, task_id: str) -> None:
        self.stopped.append(task_id)
        self.active.discard(task_id)
        self.events.append("unit:stop")

    def is_active(self, task_id: str) -> bool:
        return task_id in self.active


@pytest.fixture
def rig(tmp_path: Path):
    events: list[str] = []
    contracts = MattermostCockpitContracts(TEAM, MAIN, EXEC, OWNER, BOT)
    store = MattermostCockpitStore(db_path=tmp_path / "state.db", contracts=contracts)
    bot = FakeClient(BOT, events)
    owner = FakeClient(OWNER, events)
    source_root = {
        "id": SOURCE_ROOT,
        "channel_id": MAIN,
        "user_id": OWNER,
        "root_id": "",
        "message": "root",
        "create_at": 10,
    }
    source_post = {
        "id": SOURCE_POST,
        "channel_id": MAIN,
        "user_id": OWNER,
        "root_id": SOURCE_ROOT,
        "message": "faça a tarefa",
        "create_at": 20,
    }
    for client in (bot, owner):
        client.posts[SOURCE_ROOT] = dict(source_root)
        client.posts[SOURCE_POST] = dict(source_post)
    bridge = FakeBridge(owner)
    units = FakeUnits(events)
    service = CockpitService(
        store=store,
        contracts=contracts,
        bot_client=bot,
        owner_client=owner,
        bridge=bridge,
        units=units,
        base_url="https://mattermost.example",
        team_name="pht",
        executions_channel_name="execucoes",
        watcher_owner="test-watcher",
        state_dir=tmp_path / "watchers",
        sleep=lambda _: None,
    )
    return service, store, bot, owner, bridge, units, events


def test_create_is_idempotent_and_posts_exactly_one_execution_root(rig):
    service, store, bot, owner, bridge, units, _ = rig
    first = service.create(
        task_id="task-one",
        title="Implementar teste",
        handoff="Objetivo e critérios",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{SOURCE_POST}",
    )
    second = service.create(
        task_id="different-retry-id",
        title="Implementar teste",
        handoff="Objetivo e critérios",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{SOURCE_POST}",
    )
    assert first.task_id == second.task_id == "task-one"
    assert first.lifecycle is Lifecycle.RUNNING
    assert len([p for p in owner.posts.values() if p["channel_id"] == EXEC and not p["root_id"]]) == 1
    assert len(
        [
            p
            for p in bot.posts.values()
            if p.get("root_id") == SOURCE_ROOT and "[cockpit-link:task-one]" in p.get("message", "")
        ]
    ) == 1
    assert units.started == ["task-one"]
    assert store.get_task("task-one").execution_root_id == first.execution_root_id
    assert owner.following is False
    assert True not in owner.following_calls


def test_create_retry_restarts_a_dead_watcher_without_duplicate_root(rig):
    service, _, _, owner, _, units, _ = rig
    first = service.create(
        task_id="task-restart-dead-watcher",
        title="Restart dead watcher",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:restart-dead-watcher",
    )
    units.active.clear()

    second = service.create(
        task_id="retry-id",
        title="Restart dead watcher",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:restart-dead-watcher",
    )

    assert second.task_id == first.task_id
    assert units.started == [first.task_id, first.task_id]
    assert units.is_active(first.task_id)
    assert len([p for p in owner.posts.values() if p["channel_id"] == EXEC and not p["root_id"]]) == 1


def test_create_explicitly_unfollows_execution_root_before_starting_watcher(rig):
    service, store, _, owner, _, units, events = rig
    owner.following = True

    task = service.create(
        task_id="task-unfollow-on-create",
        title="Unfollow on create",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{SOURCE_POST}",
    )

    assert task.lifecycle is Lifecycle.RUNNING
    assert store.get_task(task.task_id).last_error is None
    assert owner.following is False
    assert owner.following_calls == [False]
    assert events.index("follow:False") < events.index("unit:start")
    assert units.started == [task.task_id]


def test_create_is_idempotent_when_execution_root_is_already_unfollowed(rig):
    service, _, _, owner, _, units, _ = rig

    task = service.create(
        task_id="task-already-unfollowed",
        title="Already unfollowed",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{SOURCE_POST}",
    )

    assert task.lifecycle is Lifecycle.RUNNING
    assert owner.following is False
    assert owner.following_calls == [False]
    assert units.started == [task.task_id]


def test_create_removes_delayed_auto_follow_after_initial_unfollowed_readback(rig):
    service, _, _, owner, _, units, _ = rig
    readbacks = iter([False, False, True, False, False, False, False, False])
    sleeps: list[float] = []
    owner.is_thread_following = lambda **_: next(readbacks)
    service.sleep = sleeps.append

    task = service.create(
        task_id="task-delayed-auto-follow",
        title="Delayed auto follow",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:delayed-auto-follow",
    )

    assert task.lifecycle is Lifecycle.RUNNING
    assert owner.following_calls == [False, False]
    assert sleeps == [0.5] * 7
    assert units.started == [task.task_id]


def test_create_retries_eventually_consistent_owner_unfollow_readback(rig):
    service, store, _, owner, _, _, _ = rig
    owner.following = True
    readbacks = iter([True, True, False, False, False, False, False])
    sleeps: list[float] = []
    owner.is_thread_following = lambda **_: next(readbacks)
    service.sleep = sleeps.append

    task = service.create(
        task_id="task-eventual-unfollow",
        title="Eventual unfollow",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{SOURCE_POST}",
    )

    assert task.lifecycle is Lifecycle.RUNNING
    assert store.get_task(task.task_id).last_error is None
    assert sleeps == [0.5] * 6
    assert owner.following_calls == [False]


def test_create_blocks_after_owner_unfollow_readback_retry_exhaustion_without_duplicate_root(rig):
    service, store, _, owner, bridge, units, _ = rig
    sleeps: list[float] = []
    owner.following = True
    owner.is_thread_following = lambda **_: True
    service.sleep = sleeps.append

    with pytest.raises(ValueError, match="owner unfollow readback mismatch"):
        service.create(
            task_id="task-unfollow-timeout",
            title="Unfollow timeout",
            handoff="handoff",
            source_channel_id=MAIN,
            source_root_id=SOURCE_ROOT,
            source_post_id=SOURCE_POST,
            dedupe_key=f"source:{SOURCE_POST}",
        )

    blocked = store.get_task("task-unfollow-timeout")
    assert blocked.lifecycle is Lifecycle.BLOCKED
    assert blocked.execution_root_id
    assert len([post for post, root_id in bridge.posts if root_id is None]) == 1
    assert units.started == []
    assert sleeps == [0.5] * 9


def test_create_recovers_blocked_unfollow_on_same_root_without_replacement(rig):
    service, store, _, owner, bridge, units, _ = rig
    owner.following = True
    original = owner.is_thread_following
    owner.is_thread_following = lambda **_: True
    service.sleep = lambda _: None

    with pytest.raises(ValueError, match="owner unfollow readback mismatch"):
        service.create(
            task_id="task-unfollow-recovery",
            title="Unfollow recovery",
            handoff="handoff",
            source_channel_id=MAIN,
            source_root_id=SOURCE_ROOT,
            source_post_id=SOURCE_POST,
            dedupe_key="source:unfollow-recovery",
        )

    blocked = store.get_task("task-unfollow-recovery")
    original_root_id = blocked.execution_root_id
    assert blocked.lifecycle is Lifecycle.BLOCKED
    assert original_root_id
    assert len([post for post, root_id in bridge.posts if root_id is None]) == 1

    owner.is_thread_following = original
    recovered = service.create(
        task_id="task-unfollow-recovery",
        title="Unfollow recovery",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:unfollow-recovery",
    )

    assert recovered.lifecycle is Lifecycle.RUNNING
    assert recovered.execution_root_id == original_root_id
    assert len([post for post, root_id in bridge.posts if root_id is None]) == 1
    assert units.started == ["task-unfollow-recovery"]


def test_create_replay_preserves_waiting_owner_state(rig):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-waiting", title="Waiting", handoff="handoff",
        source_channel_id=MAIN, source_root_id=SOURCE_ROOT, source_post_id=SOURCE_POST,
        dedupe_key="source:waiting",
    )
    waiting = service.open_gate(task.task_id, gate_id="gate-waiting", prompt="Pode aprovar?")
    posts_before = len(bot.posts)
    replay = service.create(
        task_id="different-retry-id", title="Waiting", handoff="handoff",
        source_channel_id=MAIN, source_root_id=SOURCE_ROOT, source_post_id=SOURCE_POST,
        dedupe_key="source:waiting",
    )
    assert replay.lifecycle is Lifecycle.WAITING_OWNER
    assert replay.version == waiting.version
    assert len(bot.posts) == posts_before
    assert store.get_active_gate(task.task_id).gate_id == "gate-waiting"


def test_second_active_gate_fails_before_posting_orphan_prompt(rig):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-single-gate", title="Single gate", handoff="handoff",
        source_channel_id=MAIN, source_root_id=SOURCE_ROOT, source_post_id=SOURCE_POST,
        dedupe_key="source:single-gate",
    )
    service.open_gate(task.task_id, gate_id="gate-one", prompt="Primeiro gate")
    posts_before = len(bot.posts)
    with pytest.raises(ValueError, match="active gate"):
        service.open_gate(task.task_id, gate_id="gate-two", prompt="Segundo gate")
    assert len(bot.posts) == posts_before
    assert not any("[cockpit-gate:task-single-gate:gate-two]" in post["message"] for post in bot.posts.values())
    assert store.get_active_gate(task.task_id).gate_id == "gate-one"


def test_gate_reservation_recovers_after_publish_failure(rig, monkeypatch):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-gate-recovery", title="Gate recovery", handoff="handoff",
        source_channel_id=MAIN, source_root_id=SOURCE_ROOT, source_post_id=SOURCE_POST,
        dedupe_key="source:gate-recovery",
    )
    posts_before = len(bot.posts)
    original = service._ensure_source_relay
    def fail_publish(*args, **kwargs):
        raise RuntimeError("source publish failed")
    monkeypatch.setattr(service, "_ensure_source_relay", fail_publish)
    with pytest.raises(RuntimeError, match="source publish failed"):
        service.open_gate(task.task_id, gate_id="gate-recovery", prompt="Pode aprovar?")
    reserved = store.get_active_gate(task.task_id)
    assert reserved is not None
    assert reserved.prompt_post_id == service._pending_gate_prompt_id(task.task_id, "gate-recovery")
    assert len(bot.posts) == posts_before
    assert store.get_task(task.task_id).lifecycle is Lifecycle.RUNNING
    monkeypatch.setattr(service, "_ensure_source_relay", original)
    waiting = service.open_gate(task.task_id, gate_id="gate-recovery", prompt="Pode aprovar?")
    bound = store.get_active_gate(task.task_id)
    assert waiting.lifecycle is Lifecycle.WAITING_OWNER
    assert bound is not None
    assert bound.prompt_post_id != reserved.prompt_post_id
    assert len(bot.posts) == posts_before + 1


def test_gate_retry_reuses_post_after_bind_failure(rig, monkeypatch):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-gate-bind-recovery", title="Gate bind recovery", handoff="handoff",
        source_channel_id=MAIN, source_root_id=SOURCE_ROOT, source_post_id=SOURCE_POST,
        dedupe_key="source:gate-bind-recovery",
    )
    posts_before = len(bot.posts)
    original = store.bind_gate_prompt

    def fail_bind(*args, **kwargs):
        raise RuntimeError("gate bind failed")

    monkeypatch.setattr(store, "bind_gate_prompt", fail_bind)
    with pytest.raises(RuntimeError, match="gate bind failed"):
        service.open_gate(task.task_id, gate_id="gate-bind-recovery", prompt="Pode aprovar?")
    reserved = store.get_active_gate(task.task_id)
    assert reserved is not None
    assert reserved.prompt_post_id == service._pending_gate_prompt_id(task.task_id, "gate-bind-recovery")
    assert len(bot.posts) == posts_before + 1

    monkeypatch.setattr(store, "bind_gate_prompt", original)
    waiting = service.open_gate(task.task_id, gate_id="gate-bind-recovery", prompt="Pode aprovar?")
    bound = store.get_active_gate(task.task_id)
    assert waiting.lifecycle is Lifecycle.WAITING_OWNER
    assert bound is not None
    assert bound.prompt_post_id != reserved.prompt_post_id
    assert len(bot.posts) == posts_before + 1


def test_create_reconciles_existing_marked_root_without_duplicate(rig):
    service, _, _, owner, bridge, _, _ = rig
    existing = {
        "id": EXEC_ROOT,
        "channel_id": EXEC,
        "user_id": OWNER,
        "root_id": "",
        "message": "[cockpit-task:task-two]\n# Reconciliar\n\nhandoff",
        "create_at": 30,
    }
    owner.posts[EXEC_ROOT] = existing
    owner.following = True
    task = service.create(
        task_id="task-two",
        title="Reconciliar",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:reconcile",
    )
    assert task.execution_root_id == EXEC_ROOT
    assert bridge.posts == []
    assert owner.following is False
    assert owner.following_calls == [False]


def test_create_fails_closed_on_wrong_source_author(rig):
    service, store, bot, owner, bridge, _, _ = rig
    for client in (bot, owner):
        client.posts[SOURCE_POST]["user_id"] = BOT
    with pytest.raises(ValueError, match="source author mismatch"):
        service.create(
            task_id="task-wrong",
            title="Wrong",
            handoff="handoff",
            source_channel_id=MAIN,
            source_root_id=SOURCE_ROOT,
            source_post_id=SOURCE_POST,
            dedupe_key="source:wrong",
        )
    assert store.get_task("task-wrong") is None
    assert bridge.posts == []


def test_create_persists_recoverable_blocked_state_on_tampered_source_marker(rig):
    service, store, bot, _, _, _, _ = rig
    marker_post = bot.create_post(
        MAIN,
        "[cockpit-link:task-tampered]\nWRONG",
        root_id=SOURCE_ROOT,
    )
    with pytest.raises(ValueError, match="relay body mismatch"):
        service.create(
            task_id="task-tampered",
            title="Tampered",
            handoff="handoff",
            source_channel_id=MAIN,
            source_root_id=SOURCE_ROOT,
            source_post_id=SOURCE_POST,
            dedupe_key="source:tampered",
        )
    blocked = store.get_task("task-tampered")
    assert blocked.lifecycle is Lifecycle.BLOCKED
    assert blocked.execution_root_id
    assert "relay body mismatch" in (blocked.last_error or "")

    marker_post["message"] = (
        "[cockpit-link:task-tampered]\n"
        f"Execução iniciada: [Tampered]({blocked.execution_permalink})"
    )
    resumed = service.create(
        task_id="task-tampered",
        title="Tampered",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:tampered",
    )
    assert resumed.lifecycle is Lifecycle.RUNNING
    assert resumed.last_error is None


def test_close_rejects_tampered_existing_evidence_marker(rig):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-evidence-tampered",
        title="Evidence tampered",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:evidence-tampered",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    evidence = {"tests": "5 passed"}
    encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    bot.create_post(
        EXEC,
        f"[cockpit-evidence:{task.task_id}:{digest}]\nWRONG",
        root_id=task.execution_root_id,
    )
    with pytest.raises(ValueError, match="execution relay body mismatch"):
        service.close(task.task_id, outcome=Lifecycle.SUCCEEDED, summary="done", evidence=evidence)
    pending = store.get_task(task.task_id)
    assert pending.lifecycle is Lifecycle.BLOCKED
    assert pending.cleanup_state == "cleanup_pending"


def test_close_rejects_tampered_existing_final_marker(rig):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-final-tampered",
        title="Final tampered",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:final-tampered",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    bot.create_post(MAIN, f"[cockpit-final:{task.task_id}]\nWRONG", root_id=SOURCE_ROOT)
    with pytest.raises(ValueError, match="relay body mismatch"):
        service.close(
            task.task_id,
            outcome=Lifecycle.SUCCEEDED,
            summary="done",
            evidence={"validation": "passed"},
        )
    pending = store.get_task(task.task_id)
    assert pending.lifecycle is Lifecycle.BLOCKED
    assert pending.cleanup_state == "cleanup_pending"


def test_resume_owner_decision_requires_exact_source_binding_and_dedupes(rig):
    service, _, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-decision",
        title="Decision",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:decision",
    )
    service.open_gate(task.task_id, gate_id="gate-one", prompt="Pode aprovar?")
    decision = {
        "id": DECISION_POST,
        "channel_id": MAIN,
        "user_id": OWNER,
        "root_id": SOURCE_ROOT,
        "message": "aprovado",
        "create_at": 4000,
    }
    for client in (bot, owner):
        client.posts[DECISION_POST] = dict(decision)
    with pytest.raises(ValueError, match="source root mismatch"):
        service.resume_owner_message(
            task.task_id,
            gate_id="gate-one",
            decision=GateDecision.APPROVE,
            source_root_id="z" * 26,
            source_post_id=DECISION_POST,
            message="aprovado",
        )
    before = len(bridge.posts)
    service.resume_owner_message(
        task.task_id,
        gate_id="gate-one",
        decision=GateDecision.APPROVE,
        source_root_id=SOURCE_ROOT,
        source_post_id=DECISION_POST,
        message="aprovado",
    )
    service.resume_owner_message(
        task.task_id,
        gate_id="gate-one",
        decision=GateDecision.APPROVE,
        source_root_id=SOURCE_ROOT,
        source_post_id=DECISION_POST,
        message="aprovado",
    )
    assert len(bridge.posts) == before + 1
    assert bridge.posts[-1][1] == task.execution_root_id
    assert bridge.posts[-1][0].startswith("[cockpit-decision:gate-one:approve]")


def test_resume_owner_decision_recovers_unfollow_failure_without_duplicate_relay(rig):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-decision-unfollow-recovery",
        title="Decision unfollow recovery",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:decision-unfollow-recovery",
    )
    service.open_gate(task.task_id, gate_id="gate-unfollow-recovery", prompt="Pode aprovar?")
    decision = {
        "id": DECISION_POST,
        "channel_id": MAIN,
        "user_id": OWNER,
        "root_id": SOURCE_ROOT,
        "message": "aprovado",
        "create_at": 4000,
    }
    for client in (bot, owner):
        client.posts[DECISION_POST] = dict(decision)

    original_post_owner = bridge.post_owner

    def post_owner_and_refollow(*args, **kwargs):
        result = original_post_owner(*args, **kwargs)
        owner.following = True
        owner.fail_unfollow = True
        return result

    bridge.post_owner = post_owner_and_refollow
    posts_before = len(bridge.posts)
    with pytest.raises(RuntimeError, match="unfollow failed"):
        service.resume_owner_message(
            task.task_id,
            gate_id="gate-unfollow-recovery",
            decision=GateDecision.APPROVE,
            source_root_id=SOURCE_ROOT,
            source_post_id=DECISION_POST,
            message="aprovado",
        )

    resolved = store.get_gate("gate-unfollow-recovery")
    assert resolved is not None and not resolved.active and resolved.destination_post_id
    assert store.get_task(task.task_id).lifecycle is Lifecycle.WAITING_OWNER
    assert len(bridge.posts) == posts_before + 1

    owner.fail_unfollow = False
    replay = service.resume_owner_message(
        task.task_id,
        gate_id="gate-unfollow-recovery",
        decision=GateDecision.APPROVE,
        source_root_id=SOURCE_ROOT,
        source_post_id=DECISION_POST,
        message="aprovado",
    )
    assert replay.lifecycle is Lifecycle.RUNNING
    assert owner.following is False
    assert len(bridge.posts) == posts_before + 1


def test_close_success_requires_evidence_then_relays_unfollows_stops_and_terminalizes(rig):
    service, store, bot, owner, _, units, events = rig
    task = service.create(
        task_id="task-close",
        title="Close",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:close",
    )
    with pytest.raises(ValueError, match="evidence"):
        service.close(task.task_id, outcome=Lifecycle.SUCCEEDED, summary="done", evidence={})
    owner.following = True
    closed = service.close(
        task.task_id,
        outcome=Lifecycle.SUCCEEDED,
        summary="done",
        evidence={"tests": "5 passed"},
    )
    assert closed.lifecycle is Lifecycle.SUCCEEDED
    assert owner.following_calls[-1] is False
    assert units.stopped == [task.task_id]
    assert events.index("follow:False") < events.index("unit:stop")
    assert store.get_task(task.task_id).evidence == {"tests": "5 passed"}
    source_final = next(
        p
        for p in bot.posts.values()
        if "[cockpit-final:task-close]" in p["message"]
    )
    execution_evidence = next(
        p
        for p in bot.posts.values()
        if "[cockpit-evidence:task-close:" in p["message"]
    )
    assert source_final["channel_id"] == MAIN
    assert "5 passed" not in source_final["message"]
    assert execution_evidence["channel_id"] == EXEC
    assert execution_evidence["root_id"] == task.execution_root_id
    assert "5 passed" in execution_evidence["message"]


def test_close_resumes_after_unfollow_already_completed(rig):
    service, store, bot, owner, bridge, units, _ = rig
    task = service.create(
        task_id="task-close-retry",
        title="Retry close",
        handoff="Retry partial cleanup",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{SOURCE_POST}",
    )
    owner.following = False

    closed = service.close(
        task.task_id,
        outcome=Lifecycle.SUCCEEDED,
        summary="retry completed",
        evidence={"validation": "cleanup retry passed"},
    )

    assert closed.lifecycle is Lifecycle.SUCCEEDED
    assert owner.following_calls == [False, False]
    assert units.stopped == [task.task_id]
    assert any("[cockpit-final:task-close-retry]" in p["message"] for p in bot.posts.values())


@pytest.mark.parametrize(
    ("outcome", "summary", "evidence", "last_error"),
    [
        (Lifecycle.SUCCEEDED, "completed", {"validation": "5 passed"}, None),
        (Lifecycle.FAILED, "failed", {"error": "worker failed"}, "worker failed"),
    ],
)
def test_close_clears_persisted_watcher_lease_without_watcher_finally(
    rig,
    outcome,
    summary,
    evidence,
    last_error,
):
    service, store, bot, owner, _, units, events = rig
    task = service.create(
        task_id=f"task-close-lease-{outcome.value.lower()}",
        title="Close persisted watcher lease",
        handoff="Watcher process is stopped before its finally runs",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:close-lease:{outcome.value}",
    )
    heartbeat_at = datetime.now(UTC)
    leased = store.claim_watcher(
        task.task_id,
        owner="test-watcher",
        heartbeat_at=heartbeat_at,
        stale_before=heartbeat_at - timedelta(minutes=3),
    )
    assert leased.watcher_owner == "test-watcher"
    assert leased.watcher_heartbeat_at == heartbeat_at
    assert units.is_active(task.task_id) is True
    owner.following = True
    close_events_start = len(events)

    closed = service.close(
        task.task_id,
        outcome=outcome,
        summary=summary,
        evidence=evidence,
        last_error=last_error,
    )

    close_events = events[close_events_start:]
    assert close_events.index("follow:False") < close_events.index("unit:stop")
    assert owner.following is False
    assert units.is_active(task.task_id) is False
    assert closed.lifecycle is outcome
    assert closed.watcher_owner is None
    assert closed.watcher_heartbeat_at is None
    persisted = store.get_task(task.task_id)
    assert persisted is not None
    assert persisted.watcher_owner is None
    assert persisted.watcher_heartbeat_at is None
    evidence_marker = f"[cockpit-evidence:{task.task_id}:"
    final_marker = f"[cockpit-final:{task.task_id}]"
    assert sum(evidence_marker in post["message"] for post in bot.posts.values()) == 1
    assert sum(final_marker in post["message"] for post in bot.posts.values()) == 1

    replay = service.close(
        task.task_id,
        outcome=outcome,
        summary=summary,
        evidence=evidence,
        last_error=last_error,
    )

    assert replay == closed
    assert units.stopped == [task.task_id]
    assert sum(evidence_marker in post["message"] for post in bot.posts.values()) == 1
    assert sum(final_marker in post["message"] for post in bot.posts.values()) == 1


def test_close_does_not_publish_final_result_before_cleanup_succeeds(rig):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-close-cleanup-fail",
        title="Close cleanup failure",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:close-cleanup-fail",
    )
    owner.following = True
    owner.fail_unfollow = True

    with pytest.raises(RuntimeError, match="unfollow failed"):
        service.close(
            task.task_id,
            outcome=Lifecycle.SUCCEEDED,
            summary="done",
            evidence={"tests": "5 passed"},
        )

    pending = store.get_task(task.task_id)
    assert pending.lifecycle is Lifecycle.BLOCKED
    assert pending.cleanup_state == "cleanup_pending"
    assert pending.pending_outcome is Lifecycle.SUCCEEDED
    assert "unfollow failed" in (pending.last_error or "")
    assert not any(
        "[cockpit-final:task-close-cleanup-fail]" in post["message"]
        for post in bot.posts.values()
    )

    owner.fail_unfollow = False
    closed = service.close(
        task.task_id,
        outcome=Lifecycle.SUCCEEDED,
        summary="done",
        evidence={"tests": "5 passed"},
    )
    assert closed.lifecycle is Lifecycle.SUCCEEDED
    assert closed.cleanup_state is None
    assert closed.pending_outcome is None


def test_watch_once_does_not_relay_technical_poll_output_to_source(rig):
    service, _, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-no-technical-source-relay",
        title="No technical source relay",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:no-technical-source-relay",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    bridge.poll_output = "WAKE: internal specialist log"
    source_before = dict(bot.get_thread(SOURCE_ROOT)["posts"])

    assert service.watch_once(task.task_id) is True

    assert bridge.poll_calls == 1
    assert bot.get_thread(SOURCE_ROOT)["posts"] == source_before


def test_watch_once_operates_while_owner_does_not_follow_execution_thread(rig):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-watcher-unfollowed",
        title="Watcher unfollowed",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watcher-unfollowed",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])

    assert owner.following is False
    assert service.watch_once(task.task_id) is True
    assert bridge.watch_calls == 1
    watched = store.get_task(task.task_id)
    assert watched.watcher_owner == "test-watcher"
    assert watched.watcher_heartbeat_at is not None
    assert owner.following is False
    assert owner.following_calls == [False, False, False]
    assert service.watch_once(task.task_id) is True
    assert bridge.watch_calls == 2
    assert owner.following_calls == [False, False, False, False]


def test_watch_once_removes_owner_refollow_injected_during_wake_without_close(rig):
    service, store, bot, owner, bridge, units, _ = rig
    task = service.create(
        task_id="task-watcher-refollow",
        title="Watcher refollow",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watcher-refollow",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    original_watch_main = bridge.watch_main

    def watch_main_and_refollow(*args, **kwargs):
        result = original_watch_main(*args, **kwargs)
        owner.following = True
        return result

    bridge.watch_main = watch_main_and_refollow

    assert service.watch_once(task.task_id) is True
    assert store.get_task(task.task_id).lifecycle is Lifecycle.RUNNING
    assert bridge.watch_calls == 1
    assert owner.following is False
    assert owner.following_calls == [False, False, False]
    assert units.stopped == []

def test_watch_once_exits_without_helper_call_for_terminal_task(rig):
    service, _, _, _, bridge, _, _ = rig
    task = service.create(
        task_id="task-terminal",
        title="Terminal",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:terminal",
    )
    service.close(task.task_id, outcome=Lifecycle.FAILED, summary="failed", evidence={"error": "x"})
    assert service.watch_once(task.task_id) is False
    assert bridge.watch_calls == 0


def test_watch_forever_treats_live_competing_lease_as_already_running_without_side_effects(rig):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-live-watcher",
        title="Live watcher",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:live-watcher",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    heartbeat_at = datetime.now(UTC)
    store.claim_watcher(
        task.task_id,
        owner="other-host:123",
        heartbeat_at=heartbeat_at,
        stale_before=heartbeat_at - timedelta(minutes=3),
    )
    owner.following_calls.clear()

    assert service.watch_forever(task.task_id) == "already_running"
    assert bridge.watch_calls == 0
    assert owner.following_calls == []
    assert store.get_task(task.task_id).watcher_owner == "other-host:123"


def test_local_watcher_owner_liveness_requires_matching_cockpit_task(monkeypatch):
    host = socket.gethostname()
    monkeypatch.setattr(os, "kill", lambda pid, signal: None)
    command = b"python\0-m\0hermes_cli.mattermost_cockpit\0resume\0--task\0task-one\0--watch\0"
    monkeypatch.setattr(Path, "read_bytes", lambda path: command)

    assert CockpitService._local_watcher_owner_liveness(f"{host}:123", "task-one") is True
    assert CockpitService._local_watcher_owner_liveness(f"{host}:123", "task-two") is False
    assert CockpitService._local_watcher_owner_liveness(f"{host}:123", "task-on") is False
    assert CockpitService._local_watcher_owner_liveness("remote-host:123", "task-one") is None


def test_local_watcher_owner_liveness_detects_dead_pid(monkeypatch):
    def process_missing(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", process_missing)

    assert CockpitService._local_watcher_owner_liveness(f"{socket.gethostname()}:123", "task-one") is False
