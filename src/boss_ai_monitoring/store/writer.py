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

        self.__conn = duckdb.connect(str(self._db_path))
        ensure_schema(self.__conn)
        load_views(self.__conn)
        self.__conn.execute(
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

        self.__conn.execute("BEGIN TRANSACTION")
        try:
            self.__conn.execute(f"DELETE FROM {_STAGING_TABLE}")
            self.__conn.executemany(
                f"INSERT INTO {_STAGING_TABLE} ({_INSERT_COLUMNS_SQL}) "
                f"VALUES ({_INSERT_PLACEHOLDERS_SQL})",
                rows,
            )
            before = _count_events(self.__conn)
            self.__conn.execute(
                f"INSERT INTO events SELECT s.* FROM {_STAGING_TABLE} s "
                "WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.event_id = s.event_id)"
            )
            after = _count_events(self.__conn)
            self.__conn.execute("COMMIT")
        except Exception:
            self.__conn.execute("ROLLBACK")
            raise
        return after - before

    def snapshot_to(self, dest: Path) -> Path:
        """Write a consistent point-in-time copy of the DB to ``dest``. Returns ``dest``.

        This exists because DuckDB's file lock is exclusive CROSS-process (OQ-05): while `bam
        serve` holds this write connection, no outside `duckdb`/marimo process can open the file
        at all — not even read-only. Since we hold the only write connection, we are the only one
        who can hand out a copy, and `COPY FROM DATABASE` gives a transactionally-consistent one
        (schema, tables AND views) rather than the torn bytes a plain file copy could produce.

        Runs under ``self._lock`` for the same reason ``flush()`` does (OQ-02): the whole DB
        round-trip is what needs serializing against concurrent producers, not just a swap.
        """
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(f"refusing to overwrite an existing snapshot: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # Flush buffered events first, or the snapshot silently omits whatever is still in
            # memory — a snapshot that quietly loses the newest rows is worse than no snapshot.
            if self._buffer:
                batch = self._buffer
                self._buffer = []
                self._last_flush = time.monotonic()
                self._flush_batch(batch)

            source_db = self.__conn.execute("SELECT current_database()").fetchone()
            if source_db is None:  # pragma: no cover - DuckDB always answers this
                raise RuntimeError("could not resolve the current database name")

            # ATTACH takes no bound parameters (DuckDB parser error at `?`), so the path has to
            # be a literal — escape any single quote rather than concatenate it in raw.
            dest_literal = str(dest).replace("'", "''")
            self.__conn.execute(f"ATTACH '{dest_literal}' AS bam_snapshot")
            try:
                # Identifiers can't be bound as parameters, and both sides are our own names
                # (the attach alias is a literal; the source is DuckDB's own current_database()).
                self.__conn.execute(f'COPY FROM DATABASE "{source_db[0]}" TO bam_snapshot')
            finally:
                self.__conn.execute("DETACH bam_snapshot")
        return dest

    def close(self) -> None:
        self.flush()
        self.__conn.close()
        # Evict self from the get_writer() registry so a stale, closed connection can't be
        # handed out by connect_read_only()'s writer-aware cursor() path (OQ-04) — a later
        # get_writer()/connect_read_only() call for this path opens a fresh one instead.
        with _writers_lock:
            stale_keys = [key for key, writer in _writers.items() if writer is self]
            for key in stale_keys:
                del _writers[key]

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """An INDEPENDENT read handle on this writer's connection. For READS only.

        This is the ONLY supported way for another module to read through the writer. The
        connection is name-mangled private because sharing it is what caused OQ-06: DuckDB parks
        the pending result ON the connection object, so `execute()` + `fetchone()` is not atomic —
        a second `execute()` from another thread in between makes the first caller's `fetchone()`
        return the SECOND query's rows. A cursor has its own result set and cannot be poisoned
        that way.

        Deliberately takes NO lock. A cursor sees committed rows only (MVCC) and is never blocked
        by, nor blocks, an in-flight flush — so a full-table scan through it cannot stall the
        ingest pipeline the way putting it on the write lock would. Caller owns closing it.
        """
        cursor = self.__conn.cursor()
        pin_utc(cursor)  # a cursor has its own session settings, not inherited from the parent
        return cursor

    # cursors — the ingest panes' resume points (ingest_cursors table)
    #
    # Both take ``self._lock`` for the WHOLE execute+fetch round-trip, for the same reason
    # ``flush()`` does (OQ-02/OQ-06). These run on the jsonl scanner's worker thread
    # (``scan_once`` via ``asyncio.to_thread``) while otlp and langsmith drive the same
    # connection — unlocked, ``get_cursor`` was observed returning another query's row (a bare
    # session UUID, or None), which crashed the scan pass or silently rewound a file's offset.
    def get_cursor(self, source: str, key: str) -> str | None:
        with self._lock:
            result = self.__conn.execute(
                "SELECT cursor FROM ingest_cursors WHERE source = ? AND key = ?", [source, key]
            ).fetchone()
        return result[0] if result else None

    def set_cursor(self, source: str, key: str, cursor: str) -> None:
        with self._lock:
            self.__conn.execute(
                """
                INSERT INTO ingest_cursors (source, key, cursor, updated_at)
                VALUES (?, ?, ?, now())
                ON CONFLICT (source, key) DO UPDATE SET cursor = excluded.cursor, updated_at = now()
                """,
                [source, key, cursor],
            )


_writers: dict[str, EventWriter] = {}
_writers_lock = threading.Lock()


def snapshot(db_path: Path, dest: Path) -> Path:
    """Consistent copy of the DB at ``db_path``, whether or not the app is running.

    Live writer (``bam serve`` up) -> delegate to it; it owns the only connection that can read
    the file at all. No live writer (marimo standalone, tests) -> open a short-lived one.
    """
    key = str(Path(db_path))
    with _writers_lock:
        writer = _writers.get(key)
    if writer is not None:
        return writer.snapshot_to(dest)

    standalone = EventWriter(db_path)
    try:
        return standalone.snapshot_to(dest)
    finally:
        standalone.close()


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
            return writer.cursor()
    conn = duckdb.connect(str(db_path), read_only=True)
    pin_utc(conn)
    return conn
