from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.mattermost_cockpit.contracts import GateDecision, Lifecycle, MattermostCockpitContracts
from hermes_cli.mattermost_cockpit.models import MattermostCockpitGateRelay
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

    def start(self, task_id: str) -> None:
        self.started.append(task_id)
        self.events.append("unit:start")

    def stop(self, task_id: str) -> None:
        self.stopped.append(task_id)
        self.events.append("unit:stop")

    def is_active(self, task_id: str) -> bool:
        active = task_id in self.started and task_id not in self.stopped
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
    assert units.started == ["task-one", "task-one"]
    assert store.get_task("task-one").execution_root_id == first.execution_root_id
    assert owner.following is False
    assert True not in owner.following_calls


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
    source_relay["message"] = f"{marker}\n{source_relay['message']}"
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
    assert owner.following_calls == []
    assert units.started == [task.task_id]


def test_create_removes_delayed_auto_follow_after_initial_unfollowed_readback(rig):
    service, _, _, owner, _, units, _ = rig
    readbacks = iter([False, True, False, False])
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
    assert owner.following_calls == [False]
    assert sleeps == [0.5, 0.5, 0.5]
    assert units.started == [task.task_id]


def test_create_retries_eventually_consistent_owner_unfollow_readback(rig):
    service, store, _, owner, _, _, _ = rig
    owner.following = True
    original = owner.is_thread_following
    after_set_readbacks = iter([True, True, False, False])
    sleeps: list[float] = []
    readback_calls = 0

    def delayed_readback(**kwargs):
        nonlocal readback_calls
        readback_calls += 1
        current = original(**kwargs)
        if readback_calls == 1:
            assert current is True
            return True
        assert current is False
        return next(after_set_readbacks)

    owner.is_thread_following = delayed_readback
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
    assert sleeps == [0.5, 0.5, 0.5, 0.5]
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
    readback_index = events.index("unit:is-active:false")
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
    assert False not in owner.following_calls
    assert any(
        p.get("props", {}).get("cockpit_relay_marker")
        == "[cockpit-final:task-close-retry]"
        for p in bot.posts.values()
    )


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
    assert owner.following_calls == []


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
