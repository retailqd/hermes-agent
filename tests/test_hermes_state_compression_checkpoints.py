"""Durable hierarchical-compression checkpoint primitives."""

from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def test_checkpoint_round_trip(db: SessionDB) -> None:
    db.save_compression_checkpoint("session-1", "hash-a", 0, 3, "summary one")
    db.save_compression_checkpoint("session-1", "hash-a", 1, 3, "summary two")

    latest = db.get_latest_compression_checkpoint("session-1", "hash-a", 3)

    assert latest is not None
    assert latest["chunk_index"] == 1
    assert latest["total_chunks"] == 3
    assert latest["summary"] == "summary two"


def test_checkpoint_is_append_only_and_idempotent(db: SessionDB) -> None:
    db.save_compression_checkpoint("session-1", "hash-a", 0, 2, "first write")
    db.save_compression_checkpoint("session-1", "hash-a", 0, 2, "conflicting retry")

    latest = db.get_latest_compression_checkpoint("session-1", "hash-a", 2)

    assert latest is not None
    assert latest["summary"] == "first write"


def test_checkpoint_generation_and_chunk_count_are_isolated(db: SessionDB) -> None:
    db.save_compression_checkpoint("session-1", "hash-a", 0, 2, "old generation")
    db.save_compression_checkpoint("session-1", "hash-b", 0, 4, "new generation")

    assert db.get_latest_compression_checkpoint("session-1", "hash-a", 4) is None
    latest = db.get_latest_compression_checkpoint("session-1", "hash-b", 4)
    assert latest is not None
    assert latest["summary"] == "new generation"
