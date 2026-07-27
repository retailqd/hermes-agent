"""T12: lock the compact, heading-free presentation of the main-thread reminder.

The renderer lives in the protected operational script
/home/pht2/.hermes/scripts/main_thread_reminder.py (outside the package), so
it is loaded via importlib for testing.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_SCRIPT = Path.home() / ".hermes" / "scripts" / "main_thread_reminder.py"
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")


@pytest.fixture(scope="module")
def reminder_module():
    if not _SCRIPT.exists():
        pytest.skip("main_thread_reminder.py not present on this host")
    spec = importlib.util.spec_from_file_location("main_thread_reminder", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_items() -> list[dict]:
    return [
        {
            "kind": "waiting_agent",
            "title": "Revisar catálogo da loja",
            "root_id": "a" * 26,
            "last_at": 100,
            "age_hours": 5.0,
            "family": None,
            "create_at": 100,
        },
        {
            "kind": "waiting_owner",
            "title": "Aprovar texto do rótulo",
            "root_id": "b" * 26,
            "last_at": 90,
            "age_hours": 30.0,
            "family": None,
            "create_at": 90,
        },
        {
            "kind": "open_alert",
            "title": "Linha de atendimento sem atividade",
            "root_id": "c" * 26,
            "last_at": 80,
            "age_hours": 0.5,
            "family": "down:linha",
            "create_at": 80,
        },
    ]


def test_render_is_compact_and_has_no_headings(reminder_module):
    text = reminder_module.render("https://mm.example", _sample_items())
    assert text.startswith("**Pendências do main**")
    assert not _HEADING_RE.search(text)
    assert "Ação agora" in text
    assert len(text.splitlines()) <= 18


def test_render_counts_and_links_are_present(reminder_module):
    text = reminder_module.render("https://mm.example", _sample_items())
    assert "3 abertas" in text
    assert "1 com agente" in text
    assert "1 esperando você" in text
    assert "1 alerta" in text
    assert "https://mm.example" in text


def test_render_empty_is_single_quiet_line(reminder_module):
    text = reminder_module.render("https://mm.example", [])
    assert text.startswith("**Pendências do main**")
    assert "Nada aberto" in text
    assert len(text.splitlines()) <= 4
