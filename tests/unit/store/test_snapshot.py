"""Snapshots: a consistent copy of the DB that can be read while `bam serve` holds the writer.

The point of these tests is the LIVE-WRITER case. DuckDB's file lock is exclusive cross-process,
so an external `duckdb`/marimo process cannot open the DB while the app is running (OQ-05). The
app owns the only write connection, so the app is the only thing that can hand out a consistent
copy — a plain file copy of a live DuckDB can tear.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from boss_ai_monitoring.config import BamSettings
from boss_ai_monitoring.store.writer import EventWriter, get_writer, snapshot

MakeEvent = Callable[..., dict[str, Any]]


def _read(db: Path, sql: str) -> list[tuple[Any, ...]]:
    """Open the file the way an OUTSIDE process would — read-only, no writer registry."""
    conn = duckdb.connect(str(db), read_only=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_snapshot_while_writer_is_live(
    db_path: Path, tmp_path: Path, make_event: MakeEvent
) -> None:
    """THE acceptance case: snapshot with the writer connection open and holding the file lock."""
    with EventWriter(db_path) as writer:
        writer.write_many(
            [make_event(event_id="a", source="otlp"), make_event(event_id="b", source="jsonl")]
        )
        writer.flush()

        dest = writer.snapshot_to(tmp_path / "snap.duckdb")

        # The writer is STILL live here — and the snapshot is readable from a separate connection.
        assert dest.exists()
        assert _read(dest, "SELECT count(*) FROM events") == [(2,)]


def test_snapshot_carries_views_not_just_tables(
    db_path: Path, tmp_path: Path, make_event: MakeEvent
) -> None:
    """A snapshot without the views is useless for exploration — the views are what you query."""
    with EventWriter(db_path) as writer:
        writer.write_many([make_event(event_id="a", source="otlp")])
        writer.flush()
        dest = writer.snapshot_to(tmp_path / "snap.duckdb")

    views = {
        row[0]
        for row in _read(
            dest,
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'",
        )
    }
    assert "v_sessions" in views
    assert "v_costs_daily" in views
    # and the view must actually resolve, not merely exist as a name
    _read(dest, "SELECT * FROM v_sessions")


def test_snapshot_is_a_point_in_time_copy(
    db_path: Path, tmp_path: Path, make_event: MakeEvent
) -> None:
    """Writes made after the snapshot must not leak into it."""
    with EventWriter(db_path) as writer:
        writer.write_many([make_event(event_id="a", source="otlp")])
        writer.flush()
        dest = writer.snapshot_to(tmp_path / "snap.duckdb")

        writer.write_many([make_event(event_id="b", source="otlp")])
        writer.flush()

    assert _read(dest, "SELECT count(*) FROM events") == [(1,)]


def test_module_snapshot_delegates_to_the_live_writer(
    settings: BamSettings, db_path: Path, tmp_path: Path, make_event: MakeEvent
) -> None:
    """`snapshot()` must find the live singleton, not try to open a second write connection."""
    writer = get_writer(settings)
    try:
        writer.write_many([make_event(event_id="a", source="langsmith")])
        writer.flush()

        dest = snapshot(db_path, tmp_path / "snap.duckdb")
        assert _read(dest, "SELECT count(*) FROM events") == [(1,)]
    finally:
        writer.close()


def test_module_snapshot_works_with_no_live_writer(
    db_path: Path, tmp_path: Path, make_event: MakeEvent
) -> None:
    """App down (the marimo-standalone case): open our own connection and snapshot anyway."""
    with EventWriter(db_path) as writer:
        writer.write_many([make_event(event_id="a", source="otlp")])
        writer.flush()
    # writer closed -> evicted from the registry

    dest = snapshot(db_path, tmp_path / "snap.duckdb")
    assert _read(dest, "SELECT count(*) FROM events") == [(1,)]


def test_snapshot_refuses_to_clobber_an_existing_file(
    db_path: Path, tmp_path: Path, make_event: MakeEvent
) -> None:
    existing = tmp_path / "snap.duckdb"
    existing.write_text("do not eat me")
    with EventWriter(db_path) as writer:
        writer.write_many([make_event(event_id="a", source="otlp")])
        with pytest.raises(FileExistsError):
            writer.snapshot_to(existing)
    assert existing.read_text() == "do not eat me"
