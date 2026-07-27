from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = ROOT / "docs" / "mattermost-owner-presentation.md"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "mattermost_owner_message_cases.json"
HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
EXPECTED_PREFIX = {
    "progress": "**Em andamento, ainda não concluído**",
    "next_step": "**Próximo passo, ainda não concluído**",
    "decision": "**Preciso de uma decisão sua**",
    "blocked": "**Bloqueado, ainda não concluído**",
    "completed": "**Concluído e validado**",
    "failed": "**Não concluído**",
}
REQUIRED_KINDS = {
    "decision",
    "status",
    "final",
    "failure",
    "duplicate",
    "cron",
    "watchdog",
    "progress",
    "self_review",
}


def load_cases() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def test_owner_message_fixtures_cover_the_required_cases_and_contract():
    cases = load_cases()
    kinds = {cast(str, case["kind"]) for case in cases}
    assert kinds == REQUIRED_KINDS

    for raw_case in cases:
        case = cast(dict[str, Any], raw_case)
        state = cast(str, case["state"])
        message = cast(str, case["message"])
        max_lines = cast(int, case["max_lines"])
        terminal = cast(bool, case["terminal"])

        assert state in EXPECTED_PREFIX
        assert message.startswith(EXPECTED_PREFIX[state])
        assert not HEADING_RE.search(message), case["kind"]
        assert len(message.splitlines()) <= max_lines
        assert case["allow_headings"] is False
        if terminal:
            assert state in {"completed", "failed"}
        else:
            assert state not in {"completed", "failed"}


def test_owner_message_document_explains_the_owner_contract():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for heading in [
        "Exemplos aprovados",
        "Limites",
        "Termos proibidos",
        "Exceções",
        "Revisão visual",
    ]:
        assert heading in doc
