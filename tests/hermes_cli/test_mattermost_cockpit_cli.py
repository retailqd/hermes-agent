from __future__ import annotations

import io
import json
from pathlib import Path

from hermes_cli.mattermost_cockpit.cli import main
from hermes_cli.mattermost_cockpit.contracts import GateDecision, Lifecycle


class FakeService:
    def __init__(self):
        self.calls: list[tuple] = []
        self.watcher_state = "stopped"

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return _Task("task-one", Lifecycle.RUNNING)

    def status(self, task_id=None):
        self.calls.append(("status", task_id))
        return {"task_id": task_id, "lifecycle": "RUNNING"}

    def open_gate(self, task_id, **kwargs):
        self.calls.append(("gate", task_id, kwargs))
        return _Task(task_id, Lifecycle.WAITING_OWNER)

    def resume_owner_message(self, task_id, **kwargs):
        self.calls.append(("resume", task_id, kwargs))
        return _Task(task_id, Lifecycle.RUNNING)

    def watch_forever(self, task_id):
        self.calls.append(("watch", task_id))
        return self.watcher_state

    def close(self, task_id, **kwargs):
        self.calls.append(("close", task_id, kwargs))
        return _Task(task_id, kwargs["outcome"])


class _Task:
    def __init__(self, task_id, lifecycle):
        self.task_id = task_id
        self.lifecycle = lifecycle


def _run(argv, service, stdin_text=""):
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = main(
        argv,
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
        stderr=stderr,
        service_factory=lambda: service,
    )
    return rc, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()


def test_create_reads_handoff_from_stdin_and_returns_json():
    service = FakeService()
    rc, payload, stderr = _run(
        [
            "create",
            "--task",
            "task-one",
            "--title",
            "Test",
            "--source-channel-id",
            "1" * 26,
            "--source-root-id",
            "2" * 26,
            "--source-post-id",
            "3" * 26,
        ],
        service,
        "bounded handoff",
    )
    assert rc == 0 and stderr == ""
    assert payload == {"ok": True, "task_id": "task-one", "lifecycle": "RUNNING"}
    call = service.calls[0][1]
    assert call["handoff"] == "bounded handoff"
    assert call["dedupe_key"] == "source:" + "3" * 26


def test_open_gate_reads_prompt_from_stdin():
    service = FakeService()
    rc, payload, _ = _run(
        ["gate", "--task", "task-one", "--gate-id", "gate-one", "--prompt-stdin"],
        service,
        "Pode aprovar?",
    )
    assert rc == 0 and payload is not None and payload["lifecycle"] == "WAITING_OWNER"
    assert service.calls == [
        ("gate", "task-one", {"gate_id": "gate-one", "prompt": "Pode aprovar?"})
    ]


def test_resume_owner_message_reads_stdin_and_binds_source_post():
    service = FakeService()
    rc, payload, _ = _run(
        [
            "resume",
            "--task",
            "task-one",
            "--gate-id",
            "gate-one",
            "--decision",
            "approve",
            "--source-root-id",
            "2" * 26,
            "--source-post-id",
            "4" * 26,
            "--owner-message-stdin",
        ],
        service,
        "aprovado",
    )
    assert rc == 0 and payload["ok"] is True
    assert service.calls == [
        (
            "resume",
            "task-one",
            {
                "gate_id": "gate-one",
                "decision": GateDecision.APPROVE,
                "source_root_id": "2" * 26,
                "source_post_id": "4" * 26,
                "message": "aprovado",
            },
        )
    ]


def test_duplicate_watch_reports_already_running_as_success():
    service = FakeService()
    service.watcher_state = "already_running"

    rc, payload, stderr = _run(["resume", "--task", "task-one", "--watch"], service)

    assert rc == 0 and stderr == ""
    assert payload == {"ok": True, "task_id": "task-one", "watcher": "already_running"}
    assert service.calls == [("watch", "task-one")]


def test_close_parses_evidence_file(tmp_path: Path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"tests":"6 passed"}', encoding="utf-8")
    service = FakeService()
    rc, payload, _ = _run(
        [
            "close",
            "--task",
            "task-one",
            "--outcome",
            "succeeded",
            "--summary",
            "done",
            "--evidence-file",
            str(evidence),
        ],
        service,
    )
    assert rc == 0 and payload["lifecycle"] == "SUCCEEDED"
    assert service.calls[0][2]["evidence"] == {"tests": "6 passed"}


def test_error_is_json_and_fail_closed():
    class Broken(FakeService):
        def status(self, task_id=None):
            raise ValueError("target mismatch")

    rc, payload, stderr = _run(["status", "--task", "task-one"], Broken())
    assert rc == 2 and payload is None
    assert json.loads(stderr) == {"ok": False, "error": "target mismatch"}
