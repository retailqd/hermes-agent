from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Callable, TextIO

from hermes_cli.env_loader import load_hermes_dotenv
from hermes_constants import get_hermes_home

from .client import MattermostClient
from .contracts import GateDecision, Lifecycle, MattermostCockpitContracts
from .helpers import HelperBridge
from .service import CockpitService, UnitController
from .store import MattermostCockpitStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-mattermost-cockpit")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--task")
    create.add_argument("--title", required=True)
    create.add_argument("--source-channel-id", required=True)
    create.add_argument("--source-root-id", required=True)
    create.add_argument("--source-post-id", required=True)
    create.add_argument("--handoff-file", type=Path)

    status = commands.add_parser("status")
    status.add_argument("--task")

    gate = commands.add_parser("gate")
    gate.add_argument("--task", required=True)
    gate.add_argument("--gate-id", required=True)
    gate.add_argument("--prompt-stdin", action="store_true", required=True)

    resume = commands.add_parser("resume")
    resume.add_argument("--task", required=True)
    resume.add_argument("--gate-id")
    resume.add_argument("--decision", choices=[item.value for item in GateDecision])
    mode = resume.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--owner-message-stdin", action="store_true")
    resume.add_argument("--source-root-id")
    resume.add_argument("--source-post-id")

    close = commands.add_parser("close")
    close.add_argument("--task", required=True)
    close.add_argument("--outcome", required=True, choices=("succeeded", "failed", "cancelled"))
    close.add_argument("--summary", required=True)
    close.add_argument("--evidence-file", type=Path)
    close.add_argument("--last-error")
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable is missing: {name}")
    return value


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ValueError(f"required environment variable is missing: {' or '.join(names)}")


def build_service_from_env() -> CockpitService:
    home = get_hermes_home()
    load_hermes_dotenv(hermes_home=home)
    base_url = _first_env("MATTERMOST_URL", "MATTERMOST_BASE_URL", "MATTERMOST_SERVER_URL")
    contracts = MattermostCockpitContracts(
        team_id=_required_env("MATTERMOST_COCKPIT_TEAM_ID"),
        main_channel_id=_required_env("MATTERMOST_COCKPIT_MAIN_CHANNEL_ID"),
        executions_channel_id=_required_env("MATTERMOST_COCKPIT_EXECUTIONS_CHANNEL_ID"),
        owner_author_id=_required_env("MATTERMOST_COCKPIT_OWNER_USER_ID"),
        watcher_user_id=_required_env("MATTERMOST_COCKPIT_BOT_USER_ID"),
    )
    bot = MattermostClient(base_url=base_url, token=_required_env("MATTERMOST_BOT_TOKEN"))
    owner = MattermostClient(base_url=base_url, token=_required_env("MATTERMOST_OWNER_TOKEN"))
    bridge = HelperBridge(
        owner_post_script=home.parent / ".codex/skills/mattermost-hermes/scripts/post_as_owner.py",
        poll_script=home.parent / ".codex/skills/acompanhar-main/scripts/poll_main.py",
        watch_script=home.parent / ".codex/skills/acompanhar-main/scripts/watch_main.py",
        python_bin=sys.executable,
        default_team=os.environ.get("MATTERMOST_COCKPIT_TEAM_NAME", "pht"),
        default_channel=os.environ.get("MATTERMOST_COCKPIT_EXECUTIONS_CHANNEL_NAME", "execucoes"),
    )
    db_path = Path(os.environ.get("MATTERMOST_COCKPIT_DB", MattermostCockpitStore.default_db_path()))
    store = MattermostCockpitStore(db_path=db_path, contracts=contracts)
    return CockpitService(
        store=store,
        contracts=contracts,
        bot_client=bot,
        owner_client=owner,
        bridge=bridge,
        units=UnitController(),
        base_url=base_url,
        team_name=os.environ.get("MATTERMOST_COCKPIT_TEAM_NAME", "pht"),
        executions_channel_name=os.environ.get("MATTERMOST_COCKPIT_EXECUTIONS_CHANNEL_NAME", "execucoes"),
        state_dir=db_path.parent / "watchers",
    )


def _task_payload(task) -> dict[str, object]:
    return {"ok": True, "task_id": task.task_id, "lifecycle": task.lifecycle.value}


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    service_factory: Callable[[], CockpitService] = build_service_from_env,
) -> int:
    args = _parser().parse_args(argv)
    try:
        service = service_factory()
        if args.command == "create":
            handoff = args.handoff_file.read_text(encoding="utf-8") if args.handoff_file else stdin.read()
            task_id = args.task or f"mm-{uuid.uuid4().hex[:20]}"
            task = service.create(
                task_id=task_id,
                title=args.title,
                handoff=handoff,
                source_channel_id=args.source_channel_id,
                source_root_id=args.source_root_id,
                source_post_id=args.source_post_id,
                dedupe_key=f"source:{args.source_post_id}",
            )
            payload = _task_payload(task)
        elif args.command == "status":
            payload = {"ok": True, "result": service.status(args.task)}
        elif args.command == "gate":
            task = service.open_gate(args.task, gate_id=args.gate_id, prompt=stdin.read())
            payload = _task_payload(task)
        elif args.command == "resume" and args.watch:
            service.watch_forever(args.task)
            payload = {"ok": True, "task_id": args.task, "watcher": "stopped"}
        elif args.command == "resume":
            if not args.source_root_id or not args.source_post_id or not args.gate_id or not args.decision:
                raise ValueError(
                    "owner message requires --gate-id, --decision, --source-root-id and --source-post-id"
                )
            task = service.resume_owner_message(
                args.task,
                gate_id=args.gate_id,
                decision=GateDecision(args.decision),
                source_root_id=args.source_root_id,
                source_post_id=args.source_post_id,
                message=stdin.read(),
            )
            payload = _task_payload(task)
        else:
            evidence = {}
            if args.evidence_file:
                evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
                if not isinstance(evidence, dict):
                    raise ValueError("evidence file must contain a JSON object")
            outcome = Lifecycle(args.outcome.upper())
            task = service.close(
                args.task,
                outcome=outcome,
                summary=args.summary,
                evidence=evidence,
                last_error=args.last_error,
            )
            payload = _task_payload(task)
        stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    except Exception as exc:
        message = str(exc).replace("\r", " ").replace("\n", " ")[:1000]
        stderr.write(json.dumps({"ok": False, "error": message}, ensure_ascii=False, sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
