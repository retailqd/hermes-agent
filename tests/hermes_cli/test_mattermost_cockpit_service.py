from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.mattermost_cockpit import service as service_module
from hermes_cli.mattermost_cockpit.contracts import GateDecision, Lifecycle, MattermostCockpitContracts
from hermes_cli.mattermost_cockpit.models import MattermostCockpitGateRelay
from hermes_cli.mattermost_cockpit.presentation import OwnerDecisionPrompt
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
GATE_PROMPT = """**Bloqueado**
A correção não foi aplicada porque faltou autenticação do conversor de PDF.

**Preciso de você**
Autorizar a credencial compartilhada para concluir a NF 000119.
"""
SECOND_GATE_PROMPT = """**Bloqueado**
A segunda etapa depende de autorização.

**Preciso de você**
Autorizar a segunda etapa controlada.
"""
RAW_WATCH_DIAGNOSTIC = """## 5 new posts
2026-07-24 18:14:03 retailqd post_id=abc123
[cockpit-relay:task:deadbeef]
Interrupting current task... HTTP 503 Service Unavailable
watcher: HEARTBEAT gate-auth
$ hermes-mattermost-cockpit status task-one
owner: pode seguir
"""


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
        self.update_calls: list[dict[str, object]] = []

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

    def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        props: dict | None = None,
    ) -> dict:
        self._counter += 1
        post_id = f"p{self._counter:025d}"
        post = {
            "id": post_id,
            "channel_id": channel_id,
            "user_id": self.user_id,
            "root_id": root_id or "",
            "message": message,
            "props": dict(props or {}),
            "create_at": 1000 + self._counter,
        }
        self.posts[post_id] = post
        self.events.append(f"post:{self.user_id}:{channel_id}")
        return post

    def delete_post(self, post_id: str) -> dict:
        del self.posts[post_id]
        self.events.append(f"delete:{self.user_id}:{post_id}")
        return {"status": "OK"}

    def update_post(self, post_id: str, message: str, *, props: dict | None = None) -> dict:
        post = self.posts[post_id]
        post["message"] = message
        if props is not None:
            post["props"] = dict(props)
        self.update_calls.append({"post_id": post_id, "message": message, "props": None if props is None else dict(props)})
        self.events.append(f"update:{self.user_id}:{post_id}")
        return dict(post)

    def add_reaction(self, *, user_id: str, post_id: str, emoji_name: str) -> dict:
        self.reactions = getattr(self, "reactions", [])
        self.reactions.append((user_id, post_id, emoji_name))
        return {"status": "OK"}

    def remove_reaction(self, *, user_id: str, post_id: str, emoji_name: str) -> dict:
        self.reactions = getattr(self, "reactions", [])
        self.reactions = [r for r in self.reactions if r != (user_id, post_id, emoji_name)]
        return {"status": "OK"}

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
        active = task_id in self.active
        self.events.append(f"unit:is-active:{str(active).lower()}")
        return active


def source_posts(client: FakeClient) -> list[dict]:
    return [
        post
        for post in client.posts.values()
        if post.get("root_id") == SOURCE_ROOT and post.get("user_id") == BOT
    ]


def execution_post(task, *, post_id: str, create_at: int, message: str) -> dict:
    return {
        "id": post_id,
        "channel_id": EXEC,
        "user_id": OWNER,
        "root_id": task.execution_root_id,
        "message": message,
        "create_at": create_at,
    }


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
    source_relays = [
        p
        for p in bot.posts.values()
        if p.get("root_id") == SOURCE_ROOT and p.get("user_id") == BOT
    ]
    assert len(source_relays) == 1
    source_relay = source_relays[0]
    assert source_relay["message"] == (
        "**Em andamento**\nImplementar teste\n\n"
        f"[Abrir detalhes técnicos]({service._permalink(first.execution_root_id)})"
    )
    assert "cockpit" not in source_relay["message"]
    assert source_relay["props"]["cockpit_relay_marker"] == "[cockpit-relay:task-one:started]"
    assert source_relay["props"]["cockpit_relay_schema"] == 1
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


def test_create_replays_exact_legacy_visible_marker_without_duplicate(rig):
    service, _, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-legacy",
        title="Legacy relay",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:legacy",
    )
    source_relay = next(
        post
        for post in bot.posts.values()
        if post.get("root_id") == SOURCE_ROOT and post.get("user_id") == BOT
    )
    marker = source_relay["props"].pop("cockpit_relay_marker")
    source_relay["props"].pop("cockpit_relay_schema")
    expected_message = source_relay["message"]
    source_relay["message"] = f"{marker}\n{expected_message}"
    post_ids_before = set(bot.posts)

    replayed = service.create(
        task_id="ignored-retry-id",
        title="Legacy relay",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:legacy",
    )

    assert replayed.task_id == task.task_id
    assert set(bot.posts) == post_ids_before
    assert source_relay["message"] == expected_message
    assert marker not in source_relay["message"]
    assert source_relay["props"]["cockpit_relay_marker"] == marker
    assert source_relay["props"]["cockpit_relay_schema"] == 1
    assert bot.update_calls[-1] == {
        "post_id": source_relay["id"],
        "message": source_relay["message"],
        "props": source_relay["props"],
    }


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
    waiting = service.open_gate(task.task_id, gate_id="gate-waiting", prompt=GATE_PROMPT)
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


def test_open_gate_renders_plain_owner_body_and_persists_same_prompt(rig):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-rendered-gate",
        title="Rendered gate",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:rendered-gate",
    )

    waiting = service.open_gate(task.task_id, gate_id="gate-auth", prompt=GATE_PROMPT)

    gate = store.get_active_gate(task.task_id)
    assert waiting.lifecycle is Lifecycle.WAITING_OWNER
    assert gate is not None
    gate_post = bot.get_post(gate.prompt_post_id)
    assert gate.prompt_body == gate_post["message"]
    assert gate_post["message"].startswith("**Bloqueado**")
    assert gate_post["message"].count("**Preciso de você**") == 1
    assert "gate-auth" not in gate_post["message"]
    assert gate_post["message"].endswith(
        f"[Abrir detalhes técnicos]({service._permalink(task.execution_root_id)})"
    )
    assert gate_post["props"]["cockpit_relay_marker"] == (
        "[cockpit-gate:task-rendered-gate:gate-auth]"
    )


def test_open_gate_rejects_unstructured_prompt_before_reservation_or_source_write(rig):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-invalid-gate",
        title="Invalid gate",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:invalid-gate",
    )
    source_posts_before = set(bot.posts)

    with pytest.raises(ValueError):
        service.open_gate(task.task_id, gate_id="gate-invalid", prompt="Pode aprovar?")

    assert store.get_active_gate(task.task_id) is None
    assert set(bot.posts) == source_posts_before


def test_open_gate_replays_existing_legacy_reservation_without_rewriting_body(rig):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-legacy-gate",
        title="Legacy gate",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:legacy-gate",
    )
    gate_id = "legacy-gate"
    marker = f"[cockpit-gate:{task.task_id}:{gate_id}]"
    legacy_prompt = "Pode aprovar?"
    legacy_body = f"{marker}\n{legacy_prompt}"
    now = service.now().astimezone(UTC)
    store.create_gate(
        MattermostCockpitGateRelay(
            gate_id=gate_id,
            task_id=task.task_id,
            prompt_post_id=service._pending_gate_prompt_id(task.task_id, gate_id),
            prompt_body=legacy_body,
            created_at=now,
            updated_at=now,
        )
    )

    service.open_gate(task.task_id, gate_id=gate_id, prompt=legacy_prompt)

    gate = store.get_gate(gate_id)
    assert gate is not None
    assert gate.prompt_body == legacy_body
    post = bot.get_post(gate.prompt_post_id)
    assert post["message"] == legacy_prompt
    assert post["props"]["cockpit_relay_marker"] == marker


def test_second_active_gate_fails_before_posting_orphan_prompt(rig):
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-single-gate", title="Single gate", handoff="handoff",
        source_channel_id=MAIN, source_root_id=SOURCE_ROOT, source_post_id=SOURCE_POST,
        dedupe_key="source:single-gate",
    )
    service.open_gate(task.task_id, gate_id="gate-one", prompt=GATE_PROMPT)
    posts_before = len(bot.posts)
    with pytest.raises(ValueError, match="active gate"):
        service.open_gate(task.task_id, gate_id="gate-two", prompt=SECOND_GATE_PROMPT)
    assert len(bot.posts) == posts_before
    assert not any(
        post.get("props", {}).get("cockpit_relay_marker")
        == "[cockpit-gate:task-single-gate:gate-two]"
        for post in bot.posts.values()
    )
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
        service.open_gate(task.task_id, gate_id="gate-recovery", prompt=GATE_PROMPT)
    reserved = store.get_active_gate(task.task_id)
    assert reserved is not None
    assert reserved.prompt_post_id == service._pending_gate_prompt_id(task.task_id, "gate-recovery")
    assert len(bot.posts) == posts_before
    assert store.get_task(task.task_id).lifecycle is Lifecycle.RUNNING
    monkeypatch.setattr(service, "_ensure_source_relay", original)
    waiting = service.open_gate(task.task_id, gate_id="gate-recovery", prompt=GATE_PROMPT)
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
        service.open_gate(task.task_id, gate_id="gate-bind-recovery", prompt=GATE_PROMPT)
    reserved = store.get_active_gate(task.task_id)
    assert reserved is not None
    assert reserved.prompt_post_id == service._pending_gate_prompt_id(task.task_id, "gate-bind-recovery")
    assert len(bot.posts) == posts_before + 1

    monkeypatch.setattr(store, "bind_gate_prompt", original)
    waiting = service.open_gate(task.task_id, gate_id="gate-bind-recovery", prompt=GATE_PROMPT)
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
    marker = "[cockpit-link:task-tampered]"
    marker_post = bot.create_post(
        MAIN,
        "WRONG",
        root_id=SOURCE_ROOT,
        props={"cockpit_relay_marker": marker, "cockpit_relay_schema": 1},
    )
    source_post_ids_before = {
        post_id
        for post_id, post in bot.posts.items()
        if post.get("root_id") == SOURCE_ROOT and post.get("user_id") == BOT
    }
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
    assert {
        post_id
        for post_id, post in bot.posts.items()
        if post.get("root_id") == SOURCE_ROOT and post.get("user_id") == BOT
    } == source_post_ids_before

    marker_post["message"] = f"Execução iniciada: [Tampered]({blocked.execution_permalink})"
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


@pytest.mark.parametrize(
    "props",
    [
        {"cockpit_relay_marker": "[cockpit-link:task-props]", "cockpit_relay_schema": True},
        {"cockpit_relay_marker": "[cockpit-link:task-props]", "cockpit_relay_schema": 1.0},
        {"cockpit_relay_marker": "[cockpit-link:task-props]", "cockpit_relay_schema": "1"},
        {"plugin_metadata": "unexpected"},
    ],
)
def test_create_rejects_non_integer_schema_and_nonempty_unrelated_props(rig, props):
    service, _, bot, _, _, _, _ = rig
    bot.create_post(
        MAIN,
        "[cockpit-link:task-props]\nWRONG",
        root_id=SOURCE_ROOT,
        props=props,
    )
    source_post_ids_before = set(bot.posts)

    with pytest.raises(ValueError, match="relay props mismatch"):
        service.create(
            task_id="task-props",
            title="Props validation",
            handoff="handoff",
            source_channel_id=MAIN,
            source_root_id=SOURCE_ROOT,
            source_post_id=SOURCE_POST,
            dedupe_key="source:props",
        )

    assert set(bot.posts) == source_post_ids_before


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
    service.open_gate(task.task_id, gate_id="gate-one", prompt=GATE_PROMPT)
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
    source_relay_ids_before = {
        post_id
        for post_id, post in bot.posts.items()
        if post.get("root_id") == SOURCE_ROOT and post.get("user_id") == BOT
    }
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
    assert bridge.posts[-1][0].endswith("\n" + decision["message"])
    assert {
        post_id
        for post_id, post in bot.posts.items()
        if post.get("root_id") == SOURCE_ROOT and post.get("user_id") == BOT
    } == source_relay_ids_before


def test_resume_owner_decision_reuses_post_after_resolve_failure(rig, monkeypatch):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-decision-retry",
        title="Decision retry",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:decision-retry",
    )
    service.open_gate(task.task_id, gate_id="gate-retry", prompt=GATE_PROMPT)
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
    original_resolve_gate = store.resolve_gate
    resolve_calls = 0

    def fail_once(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            raise RuntimeError("resolve failed after post")
        return original_resolve_gate(*args, **kwargs)

    monkeypatch.setattr(store, "resolve_gate", fail_once)
    with pytest.raises(RuntimeError, match="resolve failed after post"):
        service.resume_owner_message(
            task.task_id,
            gate_id="gate-retry",
            decision=GateDecision.APPROVE,
            source_root_id=SOURCE_ROOT,
            source_post_id=DECISION_POST,
            message="aprovado",
        )
    posted_count = len(bridge.posts)

    resumed = service.resume_owner_message(
        task.task_id,
        gate_id="gate-retry",
        decision=GateDecision.APPROVE,
        source_root_id=SOURCE_ROOT,
        source_post_id=DECISION_POST,
        message="aprovado",
    )

    assert resumed.lifecycle is Lifecycle.RUNNING
    assert len(bridge.posts) == posted_count
    assert store.get_gate("gate-retry").active is False


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
    service.open_gate(task.task_id, gate_id="gate-unfollow-recovery", prompt=GATE_PROMPT)
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
        title="Converter a nota fiscal para o formato solicitado.",
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
        summary="Conversão aplicada na NF 000119.",
        evidence={
            "tests": "5 passed",
            "validation": "PDF gerado e conferido no pedido correto.",
        },
    )
    assert closed.lifecycle is Lifecycle.SUCCEEDED
    assert owner.following_calls[-1] is False
    assert units.stopped == [task.task_id]
    assert events.index("follow:False") < events.index("unit:stop")
    assert store.get_task(task.task_id).evidence == {
        "tests": "5 passed",
        "validation": "PDF gerado e conferido no pedido correto.",
    }
    source_final = next(
        p
        for p in bot.posts.values()
        if p.get("props", {}).get("cockpit_relay_marker") == "[cockpit-final:task-close]"
    )
    execution_evidence = next(
        p
        for p in bot.posts.values()
        if "[cockpit-evidence:task-close:" in p["message"]
    )
    assert source_final["channel_id"] == MAIN
    assert source_final["message"].startswith("**Concluído**")
    assert "**Linguagem leiga**" in source_final["message"]
    assert "O que foi feito e o resultado atual: Conversão aplicada" in source_final["message"]
    assert "**Pendências**\nNenhuma pendência" in source_final["message"]
    assert "Esta execução foi encerrada." in source_final["message"]
    assert "**Validado**" not in source_final["message"]
    assert "[cockpit-final:" not in source_final["message"]
    assert "5 passed" not in source_final["message"]
    assert execution_evidence["channel_id"] == EXEC
    assert execution_evidence["root_id"] == task.execution_root_id
    assert "5 passed" in execution_evidence["message"]


@pytest.mark.parametrize("outcome", [Lifecycle.FAILED, Lifecycle.CANCELLED])
def test_close_interrupted_outcomes_hide_raw_last_error(rig, outcome):
    service, _, bot, owner, _, units, events = rig
    task = service.create(
        task_id=f"task-{outcome.value.lower()}",
        title="Alterar o documento conforme solicitado.",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:{outcome.value}",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    owner.following = True

    closed = service.close(
        task.task_id,
        outcome=outcome,
        summary="Execução encerrada com segurança.",
        evidence={"reason": "technical details retained"},
        last_error="RAW INTERNAL ERROR 503 secret detail",
    )

    final_post = next(
        post
        for post in bot.posts.values()
        if post.get("props", {}).get("cockpit_relay_marker")
        == f"[cockpit-final:{task.task_id}]"
    )
    assert closed.lifecycle is outcome
    assert final_post["message"].startswith("**Interrompido**")
    assert "**Linguagem leiga**" in final_post["message"]
    assert "O que foi feito e o resultado atual: Execução encerrada com segurança." in final_post["message"]
    assert "**Pendências**\nO pedido não foi concluído:" in final_post["message"]
    assert "Esta execução foi encerrada." in final_post["message"]
    assert "RAW INTERNAL ERROR" not in final_post["message"]
    assert "[cockpit-final:" not in final_post["message"]
    assert events.index("follow:False") < events.index("unit:stop")
    assert units.stopped == [task.task_id]


@pytest.mark.parametrize(
    ("outcome", "summary", "expected_pending"),
    [
        (
            Lifecycle.SUCCEEDED,
            "A conversão foi aplicada e o documento está pronto para uso.",
            "Nenhuma pendência",
        ),
        (
            Lifecycle.FAILED,
            "A conversão não foi aplicada porque o documento estava incompleto.",
            "O pedido não foi concluído: A conversão não foi aplicada porque o documento estava incompleto.",
        ),
        (
            Lifecycle.CANCELLED,
            "A execução foi cancelada antes de alterar o documento.",
            "O pedido não foi concluído: A execução foi cancelada antes de alterar o documento.",
        ),
    ],
)
def test_close_publishes_one_human_terminal_post_after_cleanup_and_retry(
    rig, outcome, summary, expected_pending
):
    service, _, bot, owner, _, units, events = rig
    task = service.create(
        task_id=f"task-human-{outcome.value.lower()}",
        title="Converter a nota fiscal para o formato solicitado.",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=f"source:human-{outcome.value.lower()}",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    owner.following = True
    evidence = {
        "validation": "pytest 135 passed",
        "technical": "HTTP 200 commit abcdef1234567",
    }
    last_error = "RAW INTERNAL ERROR 503 secret detail" if outcome is not Lifecycle.SUCCEEDED else None

    first = service.close(
        task.task_id,
        outcome=outcome,
        summary=summary,
        evidence=evidence,
        last_error=last_error,
    )
    second = service.close(
        task.task_id,
        outcome=outcome,
        summary=summary,
        evidence=evidence,
        last_error=last_error,
    )

    assert first.lifecycle is second.lifecycle is outcome
    final_posts = [
        post
        for post in source_posts(bot)
        if post.get("props", {}).get("cockpit_relay_marker") == f"[cockpit-final:{task.task_id}]"
    ]
    assert len(final_posts) == 1
    final_post = final_posts[0]
    last_public = max(source_posts(bot), key=lambda post: int(post["create_at"]))
    assert last_public["id"] == final_post["id"]
    body = final_post["message"]
    heading = "**Concluído**" if outcome is Lifecycle.SUCCEEDED else "**Interrompido**"
    assert body.startswith(heading)
    assert "**Linguagem leiga**" in body
    assert "Você pediu: Converter a nota fiscal para o formato solicitado." in body
    assert "O que foi feito e o resultado atual:" in body
    assert "**Pendências**" in body
    assert expected_pending in body
    assert ("Nenhuma pendência" in body) is (outcome is Lifecycle.SUCCEEDED)
    assert "Esta execução foi encerrada." in body
    permalink = service._permalink(task.execution_root_id)
    assert body.count(permalink) == 1
    assert body.endswith(f"[Abrir detalhes técnicos]({permalink})")
    assert body.index("**Linguagem leiga**") < body.index("**Pendências**")
    assert body.index("**Pendências**") < body.index("Esta execução foi encerrada.")
    assert body.index("Esta execução foi encerrada.") < body.index("[Abrir detalhes técnicos]")
    for forbidden in ("pytest", "HTTP", "commit", "abcdef1234567", "RAW INTERNAL", "[cockpit"):
        assert forbidden not in body

    assert units.stopped == [task.task_id]
    stop_index = events.index("unit:stop")
    readback_index = events.index("unit:is-active:false", stop_index)
    final_post_index = max(index for index, event in enumerate(events) if event == f"post:{BOT}:{MAIN}")
    assert stop_index < readback_index < final_post_index


def test_close_retry_after_public_post_does_not_duplicate_human_close(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-human-retry",
        title="Converter a nota fiscal para o formato solicitado.",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:human-retry",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    owner.following = True
    original_complete_close = store.complete_close
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated persistence interruption")
        return original_complete_close(*args, **kwargs)

    monkeypatch.setattr(store, "complete_close", fail_once)
    close_kwargs = {
        "outcome": Lifecycle.SUCCEEDED,
        "summary": "A conversão foi aplicada e o documento está pronto para uso.",
        "evidence": {"validation": "technical evidence retained"},
    }
    with pytest.raises(RuntimeError, match="persistence interruption"):
        service.close(task.task_id, **close_kwargs)
    closed = service.close(task.task_id, **close_kwargs)

    assert closed.lifecycle is Lifecycle.SUCCEEDED
    final_posts = [
        post
        for post in source_posts(bot)
        if post.get("props", {}).get("cockpit_relay_marker") == f"[cockpit-final:{task.task_id}]"
    ]
    assert len(final_posts) == 1
    assert "Esta execução foi encerrada." in final_posts[0]["message"]


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
    assert any(
        p.get("props", {}).get("cockpit_relay_marker")
        == "[cockpit-final:task-close-retry]"
        for p in bot.posts.values()
    )


@pytest.mark.parametrize(
    ("outcome", "summary", "evidence", "last_error"),
    [
        (Lifecycle.SUCCEEDED, "A limpeza terminou e está validada.", {"validation": "5 passed"}, None),
        (Lifecycle.FAILED, "A limpeza parou por um erro interno.", {"error": "worker failed"}, "worker failed"),
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
        title="Concluir a limpeza da tarefa de teste",
        handoff="O processo de acompanhamento para antes da etapa final",
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

    def final_marker_posts() -> int:
        return sum(
            post.get("props", {}).get("cockpit_relay_marker") == final_marker
            for post in bot.posts.values()
        )

    assert sum(evidence_marker in post["message"] for post in bot.posts.values()) == 1
    assert final_marker_posts() == 1
    assert not any(final_marker in post["message"] for post in bot.posts.values())

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
    assert final_marker_posts() == 1


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
        post.get("props", {}).get("cockpit_relay_marker")
        == "[cockpit-final:task-close-cleanup-fail]"
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


def test_watch_once_ignores_raw_helper_diagnostic_and_advances_cursor(rig):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-watch-raw",
        title="Semantic updates",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-raw",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["routine"] = execution_post(
        task,
        post_id="routine",
        create_at=old_cursor + 1,
        message="Interrupting current task... HTTP 503",
    )
    semantic_message = "[cockpit-owner-state]\nA conversão terminou."
    bot.posts["wrong-author"] = execution_post(
        task,
        post_id="wrong-author",
        create_at=old_cursor + 2,
        message=semantic_message,
    )
    bot.posts["wrong-author"]["user_id"] = BOT
    bot.posts["wrong-channel"] = execution_post(
        task,
        post_id="wrong-channel",
        create_at=old_cursor + 3,
        message=semantic_message,
    )
    bot.posts["wrong-channel"]["channel_id"] = MAIN
    bot.posts["wrong-root"] = execution_post(
        task,
        post_id="wrong-root",
        create_at=old_cursor + 4,
        message=semantic_message,
    )
    bot.posts["wrong-root"]["root_id"] = SOURCE_ROOT
    bridge.poll_output = RAW_WATCH_DIAGNOSTIC
    source_count = len(source_posts(bot))

    assert service.watch_once(task.task_id) is True
    assert len(source_posts(bot)) == source_count
    assert store.get_task(task.task_id).execution_cursor_ms == old_cursor + 1


def test_watch_once_relays_only_latest_semantic_execution_update(rig):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-watch-latest",
        title="Semantic latest",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-latest",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["routine"] = execution_post(
        task,
        post_id="routine",
        create_at=old_cursor + 1,
        message="Interrupting current task... HTTP 503",
    )
    bot.posts["semantic-old"] = execution_post(
        task,
        post_id="semantic-old",
        create_at=old_cursor + 2,
        message="[cockpit-owner-state]\nA autenticação foi validada e a conversão começou.",
    )
    bot.posts["semantic-latest"] = execution_post(
        task,
        post_id="semantic-latest",
        create_at=old_cursor + 3,
        message="[cockpit-owner-state]\nA conversão terminou e o PDF está em validação.",
    )
    bridge.poll_output = RAW_WATCH_DIAGNOSTIC
    prior_ids = {post["id"] for post in source_posts(bot)}

    assert service.watch_once(task.task_id) is True

    new_posts = [post for post in source_posts(bot) if post["id"] not in prior_ids]
    assert len(new_posts) == 1
    relay = new_posts[0]
    assert "A conversão terminou e o PDF está em validação." in relay["message"]
    assert "A autenticação foi validada" not in relay["message"]
    assert relay["message"].count("Abrir detalhes técnicos") == 1
    for forbidden in ("HTTP 503", "gate-auth", "post_id", "Interrupting", "cockpit"):
        assert forbidden not in relay["message"]
    assert relay["props"]["cockpit_relay_marker"] == (
        f"[cockpit-relay:{task.task_id}:update:semantic-latest]"
    )
    assert store.get_task(task.task_id).execution_cursor_ms == old_cursor + 3


def test_watch_once_aborts_if_task_closes_while_reading_execution_thread(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-close-race",
        title="Close race",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-close-race",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-race"] = execution_post(
        task,
        post_id="semantic-race",
        create_at=old_cursor + 1,
        message="[cockpit-owner-state]\nA conversão ainda estava em andamento.",
    )
    original_get_thread = bot.get_thread
    close_started = False

    def close_during_execution_read(root_id):
        nonlocal close_started
        if root_id == task.execution_root_id and not close_started:
            close_started = True
            service.close(
                task.task_id,
                outcome=Lifecycle.FAILED,
                summary="A execução foi interrompida antes de concluir a conversão.",
                evidence={"error": "interrupted"},
            )
        return original_get_thread(root_id)

    monkeypatch.setattr(bot, "get_thread", close_during_execution_read)

    assert service.watch_once(task.task_id) is False

    closed = store.get_task(task.task_id)
    assert closed.lifecycle is Lifecycle.FAILED
    assert closed.execution_cursor_ms == old_cursor
    relays = source_posts(bot)
    assert relays[-1]["props"]["cockpit_relay_marker"] == f"[cockpit-final:{task.task_id}]"
    assert not any("semantic-race" in str(post.get("props") or {}) for post in relays)


def test_watch_once_aborts_if_task_closes_while_rendering_execution_update(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-render-close-race",
        title="Close during update rendering",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-render-close-race",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-render-race"] = execution_post(
        task,
        post_id="semantic-render-race",
        create_at=old_cursor + 1,
        message="[cockpit-owner-state]\nA conversão ainda estava em andamento.",
    )
    original_render = service_module.render_execution_update
    close_started = False

    def close_during_render(message, permalink):
        nonlocal close_started
        rendered = original_render(message, permalink)
        if not close_started:
            close_started = True
            service.close(
                task.task_id,
                outcome=Lifecycle.FAILED,
                summary="A execução foi interrompida antes de concluir a conversão.",
                evidence={"error": "interrupted"},
            )
        return rendered

    monkeypatch.setattr(
        "hermes_cli.mattermost_cockpit.service.render_execution_update",
        close_during_render,
    )

    assert service.watch_once(task.task_id) is False

    closed = store.get_task(task.task_id)
    assert closed.lifecycle is Lifecycle.FAILED
    assert closed.execution_cursor_ms == old_cursor
    relays = source_posts(bot)
    assert relays[-1]["props"]["cockpit_relay_marker"] == f"[cockpit-final:{task.task_id}]"
    assert not any("semantic-render-race" in str(post.get("props") or {}) for post in relays)


def test_watch_once_aborts_relay_if_close_starts_before_source_helper(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-helper-close-race",
        title="Close before semantic relay helper",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-helper-close-race",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-helper-race"] = execution_post(
        task,
        post_id="semantic-helper-race",
        create_at=old_cursor + 1,
        message="[cockpit-owner-state]\nA conversão ainda estava em andamento.",
    )
    original_ensure_source_relay = service._ensure_source_relay
    close_started = False

    def close_before_source_helper(*args, **kwargs):
        nonlocal close_started
        marker = str(kwargs.get("marker") or "")
        if ":update:" in marker and not close_started:
            close_started = True
            service.close(
                task.task_id,
                outcome=Lifecycle.FAILED,
                summary="A execução foi interrompida antes de concluir a conversão.",
                evidence={"error": "interrupted"},
            )
        return original_ensure_source_relay(*args, **kwargs)

    monkeypatch.setattr(service, "_ensure_source_relay", close_before_source_helper)

    assert service.watch_once(task.task_id) is False

    closed = store.get_task(task.task_id)
    assert closed.lifecycle is Lifecycle.FAILED
    assert closed.execution_cursor_ms == old_cursor
    relays = source_posts(bot)
    assert relays[-1]["props"]["cockpit_relay_marker"] == f"[cockpit-final:{task.task_id}]"
    assert not any("semantic-helper-race" in str(post.get("props") or {}) for post in relays)


def test_watch_once_aborts_cursor_update_if_close_starts_after_semantic_relay(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-cursor-close-race",
        title="Close before cursor update",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-cursor-close-race",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-cursor-race"] = execution_post(
        task,
        post_id="semantic-cursor-race",
        create_at=old_cursor + 1,
        message="[cockpit-owner-state]\nA conversão ainda estava em andamento.",
    )
    original_ensure_source_relay = service._ensure_source_relay
    close_started = False

    def close_after_semantic_relay(*args, **kwargs):
        nonlocal close_started
        relay = original_ensure_source_relay(*args, **kwargs)
        marker = str(kwargs.get("marker") or "")
        if ":update:" in marker and not close_started:
            close_started = True
            service.close(
                task.task_id,
                outcome=Lifecycle.FAILED,
                summary="A execução foi interrompida antes de concluir a conversão.",
                evidence={"error": "interrupted"},
            )
        return relay

    monkeypatch.setattr(service, "_ensure_source_relay", close_after_semantic_relay)

    assert service.watch_once(task.task_id) is False

    closed = store.get_task(task.task_id)
    assert closed.lifecycle is Lifecycle.FAILED
    assert closed.execution_cursor_ms == old_cursor
    relays = source_posts(bot)
    assert relays[-2]["props"]["cockpit_relay_marker"].endswith(":update:semantic-cursor-race]")
    assert relays[-1]["props"]["cockpit_relay_marker"] == f"[cockpit-final:{task.task_id}]"


def test_watch_once_deletes_relay_if_close_starts_inside_create_post(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-create-close-race",
        title="Close inside semantic relay creation",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-create-close-race",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-create-race"] = execution_post(
        task,
        post_id="semantic-create-race",
        create_at=old_cursor + 1,
        message="[cockpit-owner-state]\nA conversão ainda estava em andamento.",
    )
    original_create_post = bot.create_post
    close_started = False

    def close_inside_create_post(channel_id, message, *, root_id=None, props=None):
        nonlocal close_started
        marker = str((props or {}).get("cockpit_relay_marker") or "")
        if ":update:" in marker and not close_started:
            close_started = True
            service.close(
                task.task_id,
                outcome=Lifecycle.FAILED,
                summary="A execução foi interrompida antes de concluir a conversão.",
                evidence={"error": "interrupted"},
            )
        return original_create_post(channel_id, message, root_id=root_id, props=props)

    monkeypatch.setattr(bot, "create_post", close_inside_create_post)

    assert service.watch_once(task.task_id) is False

    closed = store.get_task(task.task_id)
    assert closed.lifecycle is Lifecycle.FAILED
    assert closed.execution_cursor_ms == old_cursor
    relays = source_posts(bot)
    assert relays[-1]["props"]["cockpit_relay_marker"] == f"[cockpit-final:{task.task_id}]"
    assert not any("semantic-create-race" in str(post.get("props") or {}) for post in relays)


def test_watch_once_handles_close_winning_inside_cursor_cas(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-cas-close-race",
        title="Close during cursor comparison",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-cas-close-race",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-cas-race"] = execution_post(
        task,
        post_id="semantic-cas-race",
        create_at=old_cursor + 1,
        message="Rotina interna sem marcador semântico.",
    )
    original_update_cursors = store.update_cursors
    close_started = False

    def close_inside_cursor_cas(*args, **kwargs):
        nonlocal close_started
        if not close_started:
            close_started = True
            service.close(
                task.task_id,
                outcome=Lifecycle.FAILED,
                summary="A execução foi interrompida antes de concluir a conversão.",
                evidence={"error": "interrupted"},
            )
        return original_update_cursors(*args, **kwargs)

    monkeypatch.setattr(store, "update_cursors", close_inside_cursor_cas)

    assert service.watch_once(task.task_id) is False

    closed = store.get_task(task.task_id)
    assert closed.lifecycle is Lifecycle.FAILED
    assert closed.execution_cursor_ms == old_cursor
    relays = source_posts(bot)
    assert relays[-1]["props"]["cockpit_relay_marker"] == f"[cockpit-final:{task.task_id}]"


def test_watch_once_renders_semantic_blocker(rig):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-blocked",
        title="Semantic blocker",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-blocked",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-blocker"] = execution_post(
        task,
        post_id="semantic-blocker",
        create_at=old_cursor + 1,
        message=(
            "[cockpit-owner-blocked]\n**Bloqueado**\n"
            "A conversão depende de uma credencial.\n\n"
            "**Preciso de você**\nAutorizar a credencial compartilhada."
        ),
    )
    source_count = len(source_posts(bot))

    assert service.watch_once(task.task_id) is True

    relay = source_posts(bot)[source_count]
    assert relay["message"].startswith("**Bloqueado**")
    assert relay["message"].count("**Preciso de você**") == 1
    assert relay["message"].count("Abrir detalhes técnicos") == 1
    assert store.get_task(task.task_id).execution_cursor_ms == old_cursor + 1


def test_watch_once_ignores_malformed_semantic_update_but_advances_cursor(rig):
    service, store, bot, owner, _, _, _ = rig
    task = service.create(
        task_id="task-watch-malformed",
        title="Malformed semantic update",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:watch-malformed",
    )
    bot.posts[task.execution_root_id] = dict(owner.posts[task.execution_root_id])
    old_cursor = task.execution_cursor_ms
    bot.posts["semantic-malformed"] = execution_post(
        task,
        post_id="semantic-malformed",
        create_at=old_cursor + 1,
        message="[cockpit-owner-state]\nA chamada retornou 401 Unauthorized.",
    )
    source_count = len(source_posts(bot))

    assert service.watch_once(task.task_id) is True
    assert len(source_posts(bot)) == source_count
    assert store.get_task(task.task_id).execution_cursor_ms == old_cursor + 1


def test_watch_once_operates_while_owner_does_not_follow_execution_thread(rig):
    service, store, bot, owner, bridge, _, _ = rig
    task = service.create(
        task_id="task-watcher-unfollowed",
        title="Owner thread unfollowed",
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
        title="Acompanhar a execução solicitada.",
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
        title="Alterar o documento solicitado.",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:terminal",
    )
    service.close(
        task.task_id,
        outcome=Lifecycle.FAILED,
        summary="A tarefa foi interrompida antes de terminar.",
        evidence={"error": "x"},
    )
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


# ---------------------------------------------------------------------------
# T7: structured lay-language gates + terminal lifecycle invariants
# ---------------------------------------------------------------------------


def _t7_running_task(service, task_id, dedupe):
    return service.create(
        task_id=task_id,
        title="Ajustar o catálogo de produtos da loja",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key=dedupe,
    )


def _t7_decision():
    return OwnerDecisionPrompt(
        decision="Reprocessar itens incompletos",
        plain_language="Vou atualizar somente os itens listados, sem criar pedidos novos.",
        risk="Baixo e restrito aos itens listados.",
        reply_instruction="Responda `aprovar` para continuar ou diga o ajuste.",
    )


def test_open_gate_structured_renders_plain_language_contract(rig):
    service, store, bot, _, _, _, _ = rig
    task = _t7_running_task(service, "task-gate-structured", "source:gate-structured")
    service.open_gate(task.task_id, gate_id="gate-structured", decision=_t7_decision())
    gate_posts = [
        p
        for p in bot.posts.values()
        if p.get("root_id") == SOURCE_ROOT
        and "Preciso de uma decisão sua" in p.get("message", "")
    ]
    assert len(gate_posts) == 1
    body = gate_posts[0]["message"]
    assert body.startswith("**Preciso de uma decisão sua**")
    assert "**Em linguagem simples:**" in body
    assert "**Risco:**" in body
    assert "**Como responder:**" in body
    assert "##" not in body
    assert "[cockpit" not in body
    assert store.get_task(task.task_id).lifecycle is Lifecycle.WAITING_OWNER


def test_open_gate_requires_exactly_one_prompt_form(rig):
    service, _, _, _, _, _, _ = rig
    task = _t7_running_task(service, "task-gate-forms", "source:gate-forms")
    with pytest.raises(ValueError, match="exactly one"):
        service.open_gate(
            task.task_id, gate_id="gate-both", prompt="texto", decision=_t7_decision()
        )
    with pytest.raises(ValueError, match="exactly one"):
        service.open_gate(task.task_id, gate_id="gate-none")


@pytest.mark.parametrize("outcome", [Lifecycle.SUCCEEDED, Lifecycle.FAILED, Lifecycle.CANCELLED])
def test_terminal_task_rejects_new_gate(rig, outcome):
    service, _, _, _, _, _, _ = rig
    task = _t7_running_task(
        service,
        f"task-terminal-gate-{outcome.value.lower()}",
        f"source:terminal-gate:{outcome.value}",
    )
    service.close(
        task.task_id,
        outcome=outcome,
        summary="A tarefa terminou durante o teste.",
        evidence={"validation": "test"},
        last_error=None,
    )
    with pytest.raises(ValueError, match="terminal"):
        service.open_gate(task.task_id, gate_id="late-gate", decision=_t7_decision())


def test_close_resolves_active_gate_before_terminal_transition(rig):
    service, store, _, _, _, _, _ = rig
    task = _t7_running_task(service, "task-close-gate", "source:close-gate")
    service.open_gate(task.task_id, gate_id="gate-close", decision=_t7_decision())
    assert store.get_active_gate(task.task_id) is not None

    service.close(
        task.task_id,
        outcome=Lifecycle.CANCELLED,
        summary="Encerrada durante o teste de limpeza.",
        evidence={"validation": "test"},
        last_error=None,
    )

    assert store.get_active_gate(task.task_id) is None
    assert store.get_task(task.task_id).lifecycle is Lifecycle.CANCELLED
    events = store.list_audit_events(task.task_id)
    assert any(e.event_type == "gate_resolved_terminal" for e in events)


def test_close_falls_back_to_neutral_request_when_title_has_jargon(rig):
    # Legacy autopilot tasks carry technical titles; close must still succeed
    # with a neutral lay request line instead of failing closed forever.
    service, store, bot, _, _, _, _ = rig
    task = service.create(
        task_id="task-legacy-title",
        title="INC-GP gateway watcher deploy f350f4c4",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:legacy-title",
    )
    closed = service.close(
        task.task_id,
        outcome=Lifecycle.CANCELLED,
        summary="Encerrada na limpeza geral por falta de atividade.",
        evidence={"validation": "test"},
        last_error=None,
    )
    assert closed.lifecycle is Lifecycle.CANCELLED
    final_posts = [
        p
        for p in bot.posts.values()
        if p.get("props", {}).get("cockpit_relay_marker") == "[cockpit-final:task-legacy-title]"
    ]
    assert len(final_posts) == 1
    body = final_posts[0]["message"]
    assert "o pedido registrado nesta conversa" in body
    assert "gateway" not in body
    assert "watcher" not in body


# ---------------------------------------------------------------------------
# T9: single owner status post per task, edited in place
# ---------------------------------------------------------------------------


def test_relay_status_upserts_single_post_in_place(rig):
    service, store, bot, _, _, _, events = rig
    task = _t7_running_task(service, "task-status-upsert", "source:status-upsert")

    first = service.relay_status(
        task.task_id,
        now_text="estou revisando os dados antes de mexer em qualquer coisa",
        next_milestone="volto quando a causa estiver confirmada",
    )
    second = service.relay_status(
        task.task_id,
        now_text="a causa foi confirmada e o ajuste está sendo preparado",
        next_milestone="volto quando o ajuste estiver validado",
    )

    assert first["post_id"] == second["post_id"]
    status_posts = [
        p
        for p in bot.posts.values()
        if p.get("props", {}).get("cockpit_relay_marker") == f"[cockpit-status:{task.task_id}]"
    ]
    assert len(status_posts) == 1
    body = status_posts[0]["message"]
    assert body.startswith("**Em andamento, ainda não concluído**")
    assert "a causa foi confirmada" in body.lower()
    assert "[cockpit" not in body
    assert events.count(f"update:{BOT}:{first['post_id']}") == 1
    assert bot.update_calls[-1]["props"] == status_posts[0]["props"]


def test_relay_status_same_text_does_not_edit_again(rig):
    service, _, _, _, _, _, events = rig
    task = _t7_running_task(service, "task-status-noop", "source:status-noop")
    kwargs = {
        "now_text": "estou revisando os dados",
        "next_milestone": "volto quando houver novidade concreta",
    }
    first = service.relay_status(task.task_id, **kwargs)
    service.relay_status(task.task_id, **kwargs)
    assert events.count(f"update:{BOT}:{first['post_id']}") == 0


def test_relay_status_rejected_for_terminal_task(rig):
    service, _, _, _, _, _, _ = rig
    task = _t7_running_task(service, "task-status-terminal", "source:status-terminal")
    service.close(
        task.task_id,
        outcome=Lifecycle.CANCELLED,
        summary="Encerrada durante o teste.",
        evidence={"validation": "test"},
        last_error=None,
    )
    with pytest.raises(ValueError, match="terminal"):
        service.relay_status(
            task.task_id,
            now_text="não deveria postar",
            next_milestone="nunca",
        )


def test_reaper_opens_one_structured_gate_for_stale_task(rig):
    service, store, bot, owner, _, _, _ = rig
    now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
    service.now = lambda: now
    task = service.create(
        task_id="task-reaper-stale",
        title="Revisar execução parada",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:reaper-stale",
    )
    root = dict(owner.posts[task.execution_root_id])
    root["create_at"] = int((now - timedelta(hours=25)).timestamp() * 1000)
    bot.posts[task.execution_root_id] = root

    first = service.reap_stale()
    second = service.reap_stale()

    assert first == {
        "closed": [],
        "asked": [task.task_id],
        "waiting": [],
        "active": [],
        "errors": {},
    }
    assert second == {
        "closed": [],
        "asked": [],
        "waiting": [task.task_id],
        "active": [],
        "errors": {},
    }
    waiting = store.get_task(task.task_id)
    assert waiting is not None and waiting.lifecycle is Lifecycle.WAITING_OWNER
    gate = store.get_active_gate(task.task_id)
    assert gate is not None
    prompt = bot.get_post(gate.prompt_post_id)["message"]
    assert prompt.startswith("**Preciso de uma decisão sua**")
    assert "continuar ou encerrar" in prompt


def test_reaper_ignores_task_with_recent_thread_activity(rig):
    service, _, bot, owner, _, _, _ = rig
    now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
    service.now = lambda: now
    task = service.create(
        task_id="task-reaper-recent",
        title="Execução recente",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:reaper-recent",
    )
    root = dict(owner.posts[task.execution_root_id])
    root["create_at"] = int((now - timedelta(hours=1)).timestamp() * 1000)
    bot.posts[task.execution_root_id] = root

    assert service.reap_stale() == {
        "closed": [],
        "asked": [],
        "waiting": [],
        "active": [task.task_id],
        "errors": {},
    }


def test_reaper_finishes_stale_cleanup_pending_task(rig):
    service, store, bot, owner, _, units, _ = rig
    now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
    service.now = lambda: now
    task = service.create(
        task_id="task-reaper-cleanup",
        title="Finalizar limpeza",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:reaper-cleanup",
    )
    root = dict(owner.posts[task.execution_root_id])
    root["create_at"] = int((now - timedelta(hours=25)).timestamp() * 1000)
    bot.posts[task.execution_root_id] = root
    prepared = store.prepare_close(
        task.task_id,
        expected_version=task.version,
        outcome=Lifecycle.CANCELLED,
        result_summary="Execução cancelada",
        evidence={},
        last_error=None,
    )
    assert prepared.cleanup_state is not None

    result = service.reap_stale()

    assert result["closed"] == [task.task_id]
    closed = store.get_task(task.task_id)
    assert closed is not None and closed.lifecycle is Lifecycle.CANCELLED
    assert task.task_id in units.stopped


def test_reaper_isolates_thread_failure_and_continues(rig, monkeypatch):
    service, store, bot, owner, _, _, _ = rig
    now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
    service.now = lambda: now
    first = service.create(
        task_id="task-reaper-broken",
        title="Execução inacessível",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:reaper-broken",
    )
    second = service.create(
        task_id="task-reaper-healthy",
        title="Execução parada",
        handoff="handoff",
        source_channel_id=MAIN,
        source_root_id=SOURCE_ROOT,
        source_post_id=SOURCE_POST,
        dedupe_key="source:reaper-healthy",
    )
    for task in (first, second):
        root = dict(owner.posts[task.execution_root_id])
        root["create_at"] = int((now - timedelta(hours=25)).timestamp() * 1000)
        bot.posts[task.execution_root_id] = root

    original_get_thread = bot.get_thread

    def selective_get_thread(root_id):
        if root_id == first.execution_root_id:
            raise RuntimeError("thread unavailable")
        return original_get_thread(root_id)

    monkeypatch.setattr(bot, "get_thread", selective_get_thread)

    result = service.reap_stale()

    assert result["errors"] == {first.task_id: "RuntimeError: thread unavailable"}
    assert result["asked"] == [second.task_id]
    assert store.get_active_gate(second.task_id) is not None


# ---------------------------------------------------------------------------
# Ciclo fechado da sala Ordens: close notifica a origem
# ---------------------------------------------------------------------------


def _close_kwargs():
    return {
        "outcome": Lifecycle.SUCCEEDED,
        "summary": "A tarefa terminou e está validada.",
        "evidence": {"validation": "test"},
        "last_error": None,
    }


def test_close_notifies_ordens_room_when_origin_marker_present(rig, monkeypatch):
    service, _, bot, _, _, _, _ = rig
    monkeypatch.setenv("ORDENS_MATRIX_ROOM", "!room:example.org")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_send_matrix_notice", lambda room, text: sent.append((room, text)))
    task = _t7_running_task(service, "task-ordens-origin", "source:ordens-origin")
    bot.posts[task.source_root_id]["message"] += "\n**Origem:** conversa no Ordens (Matrix), 2026-07-27"

    service.close(task.task_id, **_close_kwargs())

    assert len(sent) == 1
    room, text = sent[0]
    assert room == "!room:example.org"
    assert "Concluído e validado" in text
    assert task.title in text


def test_close_does_not_notify_without_origin_marker(rig, monkeypatch):
    service, _, _, _, _, _, _ = rig
    monkeypatch.setenv("ORDENS_MATRIX_ROOM", "!room:example.org")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_send_matrix_notice", lambda room, text: sent.append((room, text)))
    task = _t7_running_task(service, "task-sem-origem", "source:sem-origem")

    service.close(task.task_id, **_close_kwargs())

    assert sent == []


def test_close_never_fails_because_of_origin_notice(rig, monkeypatch):
    service, store, bot, _, _, _, _ = rig
    monkeypatch.setenv("ORDENS_MATRIX_ROOM", "!room:example.org")

    def boom(room, text):
        raise RuntimeError("matrix fora do ar")

    monkeypatch.setattr(service, "_send_matrix_notice", boom)
    task = _t7_running_task(service, "task-ordens-boom", "source:ordens-boom")
    bot.posts[task.source_root_id]["message"] += "\n**Origem:** conversa no Ordens (Matrix), 2026-07-27"

    closed = service.close(task.task_id, **_close_kwargs())

    assert closed.lifecycle is Lifecycle.SUCCEEDED
    assert store.get_task(task.task_id).lifecycle is Lifecycle.SUCCEEDED


def test_open_gate_notifies_ordens_room_when_origin_marker_present(rig, monkeypatch):
    service, _, bot, _, _, _, _ = rig
    monkeypatch.setenv("ORDENS_MATRIX_ROOM", "!room:example.org")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_send_matrix_notice", lambda room, text: sent.append((room, text)))
    task = _t7_running_task(service, "task-gate-ordens", "source:gate-ordens")
    bot.posts[task.source_root_id]["message"] += "\n**Origem:** conversa no Ordens (Matrix), 2026-07-27"

    service.open_gate(task.task_id, gate_id="gate-ordens", decision=_t7_decision())

    gate_notices = [t for _, t in sent if t.startswith("🚦")]
    assert len(gate_notices) == 1
    assert "Preciso de uma decisão sua" in gate_notices[0]

    # replay idempotente do mesmo gate não re-notifica
    service.open_gate(task.task_id, gate_id="gate-ordens", decision=_t7_decision())
    assert len([t for _, t in sent if t.startswith("🚦")]) == 1


def test_open_gate_does_not_notify_without_origin_marker(rig, monkeypatch):
    service, _, _, _, _, _, _ = rig
    monkeypatch.setenv("ORDENS_MATRIX_ROOM", "!room:example.org")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_send_matrix_notice", lambda room, text: sent.append((room, text)))
    task = _t7_running_task(service, "task-gate-sem-origem", "source:gate-sem-origem")

    service.open_gate(task.task_id, gate_id="gate-sem-origem", decision=_t7_decision())

    assert sent == []


def test_open_gate_never_fails_because_of_origin_notice(rig, monkeypatch):
    service, store, bot, _, _, _, _ = rig
    monkeypatch.setenv("ORDENS_MATRIX_ROOM", "!room:example.org")

    def boom(room, text):
        raise RuntimeError("matrix fora do ar")

    monkeypatch.setattr(service, "_send_matrix_notice", boom)
    task = _t7_running_task(service, "task-gate-boom", "source:gate-boom")
    bot.posts[task.source_root_id]["message"] += "\n**Origem:** conversa no Ordens (Matrix), 2026-07-27"

    current = service.open_gate(task.task_id, gate_id="gate-boom", decision=_t7_decision())

    assert current.lifecycle is Lifecycle.WAITING_OWNER
    assert store.get_active_gate(task.task_id) is not None


# ---------------------------------------------------------------------------
# Lógica de fechamento: selo na raiz + revisão do dono
# ---------------------------------------------------------------------------


def test_close_succeeded_marks_root_pending_review(rig):
    service, _, bot, _, _, _, _ = rig
    task = _t7_running_task(service, "task-seal-eyes", "source:seal-eyes")
    service.close(task.task_id, **_close_kwargs())
    assert (service.contracts.watcher_user_id, task.source_root_id, "eyes") in getattr(bot, "reactions", [])


def test_close_cancelled_seals_root_directly(rig):
    service, _, bot, _, _, _, _ = rig
    task = _t7_running_task(service, "task-seal-cancel", "source:seal-cancel")
    service.close(
        task.task_id,
        outcome=Lifecycle.CANCELLED,
        summary="Encerrada durante o teste.",
        evidence={"validation": "test"},
        last_error=None,
    )
    assert (service.contracts.watcher_user_id, task.source_root_id, "no_entry_sign") in getattr(bot, "reactions", [])


def test_seal_reviewed_swaps_eyes_for_check(rig):
    service, _, bot, _, _, _, _ = rig
    task = _t7_running_task(service, "task-seal-review", "source:seal-review")
    service.close(task.task_id, **_close_kwargs())

    result = service.seal_reviewed(task.task_id)

    reactions = getattr(bot, "reactions", [])
    assert (service.contracts.watcher_user_id, task.source_root_id, "eyes") not in reactions
    assert (service.contracts.watcher_user_id, task.source_root_id, "white_check_mark") in reactions
    assert result["sealed"] == "white_check_mark"


def test_seal_reviewed_rejects_open_task(rig):
    service, _, _, _, _, _, _ = rig
    task = _t7_running_task(service, "task-seal-open", "source:seal-open")
    with pytest.raises(ValueError, match="terminal"):
        service.seal_reviewed(task.task_id)
