"""THE single write connection to the DuckDB file (G5). Nobody else opens one.

``EventWriter`` buffers events and flushes them in one atomic, idempotent transaction: a batch is
staged, then merged into ``events`` with an anti-join on ``event_id`` so re-flushing an
already-written id writes zero new rows. Everyone else reads via :func:`connect_read_only`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

from boss_ai_monitoring.store.schema import EVENT_COLUMNS, ensure_schema, load_views, pin_utc

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boss_ai_monitoring.config import BamSettings

Event = dict[str, Any]

_INSERT_COLUMNS_SQL = ", ".join(EVENT_COLUMNS)
_INSERT_PLACEHOLDERS_SQL = ", ".join(["?"] * len(EVENT_COLUMNS))
_STAGING_TABLE = "_events_staging"


def _event_to_row(event: Event) -> tuple[Any, ...]:
    payload = event.get("payload") or {}
    return tuple(
        json.dumps(payload) if column == "payload" else event.get(column)
        for column in EVENT_COLUMNS
    )


def _count_events(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute("SELECT count(*) FROM events").fetchone()
    assert row is not None
    return row[0]


class EventWriter:
    """THE single write connection to the DuckDB file. Nobody else opens one (G5)."""

    def __init__(
        self,
        db_path: Path,
        *,
        batch_size: int = 500,
        flush_interval_ms: int = 1000,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._batch_size = batch_size
        self._flush_interval_ms = flush_interval_ms
        self._buffer: list[Event] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

        self._conn = duckdb.connect(str(self._db_path))
        ensure_schema(self._conn)
        load_views(self._conn)
        self._conn.execute(
            f"CREATE TEMP TABLE {_STAGING_TABLE} AS SELECT * FROM events WHERE FALSE"
        )

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write(self, event: Event) -> None:
        """Buffer one event. Flushes when batch_size or flush_interval_ms is hit."""
        with self._lock:
            self._buffer.append(event)
            elapsed_ms = (time.monotonic() - self._last_flush) * 1000
            should_flush = (
                len(self._buffer) >= self._batch_size or elapsed_ms >= self._flush_interval_ms
            )
        if should_flush:
            self.flush()

    def write_many(self, events: Iterable[Event]) -> int:
        """Buffer many; returns the count buffered."""
        buffered = list(events)
        with self._lock:
            self._buffer.extend(buffered)
            should_flush = len(self._buffer) >= self._batch_size
        if should_flush:
            self.flush()
        return len(buffered)

    def flush(self) -> int:
        """Force a flush. Returns rows written. ATOMIC and IDEMPOTENT on event_id.

        Holds ``self._lock`` for the buffer swap AND the DB round-trip below (OQ-02): the shared
        connection's transaction is what needs serializing across concurrent callers of the
        `get_writer()` singleton (otlp/jsonl/langsmith), not just the list mutation. Releasing
        the lock before `_flush_batch` let two threads both pass the swap and then race
        `BEGIN TRANSACTION` on the one connection.
        """
        with self._lock:
            batch = self._buffer
            self._buffer = []
            self._last_flush = time.monotonic()
            if not batch:
                return 0
            return self._flush_batch(batch)

    def _flush_batch(self, batch: list[Event]) -> int:
        deduped: dict[Any, tuple[Any, ...]] = {}
        for event in batch:
            row = _event_to_row(event)
            deduped[row[0]] = row  # last write in the batch wins
        rows = list(deduped.values())

        self._conn.execute("BEGIN TRANSACTION")
        try:
            self._conn.execute(f"DELETE FROM {_STAGING_TABLE}")
            self._conn.executemany(
                f"INSERT INTO {_STAGING_TABLE} ({_INSERT_COLUMNS_SQL}) "
                f"VALUES ({_INSERT_PLACEHOLDERS_SQL})",
                rows,
            )
            before = _count_events(self._conn)
            self._conn.execute(
                f"INSERT INTO events SELECT s.* FROM {_STAGING_TABLE} s "
                "WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.event_id = s.event_id)"
            )
            after = _count_events(self._conn)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return after - before

    def close(self) -> None:
        self.flush()
        self._conn.close()
        # Evict self from the get_writer() registry so a stale, closed connection can't be
        # handed out by connect_read_only()'s writer-aware cursor() path (OQ-04) — a later
        # get_writer()/connect_read_only() call for this path opens a fresh one instead.
        with _writers_lock:
            stale_keys = [key for key, writer in _writers.items() if writer is self]
            for key in stale_keys:
                del _writers[key]

    # cursors — the ingest panes' resume points (ingest_cursors table)
    def get_cursor(self, source: str, key: str) -> str | None:
        result = self._conn.execute(
            "SELECT cursor FROM ingest_cursors WHERE source = ? AND key = ?", [source, key]
        ).fetchone()
        return result[0] if result else None

    def set_cursor(self, source: str, key: str, cursor: str) -> None:
        self._conn.execute(
            """
            INSERT INTO ingest_cursors (source, key, cursor, updated_at)
            VALUES (?, ?, ?, now())
            ON CONFLICT (source, key) DO UPDATE SET cursor = excluded.cursor, updated_at = now()
            """,
            [source, key, cursor],
        )


_writers: dict[str, EventWriter] = {}
_writers_lock = threading.Lock()


def get_writer(settings: BamSettings) -> EventWriter:
    """Process-wide singleton. Serializes concurrent writers behind ONE connection — safe to call
    from the OTLP route handler, the JSONL scanner, and the LangSmith poller at the same time."""
    key = str(settings.store.db_path)
    with _writers_lock:
        writer = _writers.get(key)
        if writer is None:
            writer = EventWriter(
                settings.store.db_path,
                batch_size=settings.store.batch_size,
                flush_interval_ms=settings.store.flush_interval_ms,
            )
            _writers[key] = writer
        return writer


def connect_read_only(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Everyone READING (web, jobs, the marimo notebook, tests) uses THIS — a read-only
    connection, so it never fights the single writer.

    Writer-aware (OQ-04): DuckDB refuses a second `duckdb.connect(path, read_only=True)` while a
    non-read-only connection to the same file is already open in-process — exactly the situation
    `bam serve` is in the moment `get_writer()`'s singleton goes live. When a writer for this path
    is already live, hand back a `.cursor()` off that SAME connection instead: cursors support
    concurrent queries via MVCC (see committed rows only, never blocked by or blocking the
    writer's in-flight transaction) and don't hit the config-mismatch check. Falls back to a
    fresh read-only connect when no writer is live (tests, the marimo notebook standalone).
    """
    key = str(Path(db_path))
    with _writers_lock:
        writer = _writers.get(key)
        if writer is not None:
            # Hold the lock across the cursor() call too, not just the lookup: close() also
            # takes _writers_lock to evict itself, so this can't hand out a cursor on a
            # connection that's mid-close.
            cursor = writer._conn.cursor()
            pin_utc(cursor)  # a cursor has its own session settings, not inherited from parent
            return cursor
    conn = duckdb.connect(str(db_path), read_only=True)
    pin_utc(conn)
    return conn
