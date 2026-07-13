# Hotfix: shared-DuckDB-connection race + three silent data bugs

**Date:** 2026-07-12 · **Branch:** `feature-hotfix-langsmith` · **Trigger:** `just dev` crashed on boot

Four bugs, all live in production, all invisible to a **green 256-test suite**. That last part is
the finding that matters most: the tests exercise every component in isolation and never the
concurrent whole, so the suite agreed the system was healthy while the JSONL scanner was crash-
looping, one job had never once produced its primary signal, and the OTLP receiver 500'd on
wrong-shaped input.

---

## OQ-06 (P0) — `get_cursor()` returned another query's result set

### Symptom

```
ERROR boss_ai_monitoring.ingest.jsonl: jsonl: scan pass failed, will retry next interval
  File "src/boss_ai_monitoring/ingest/jsonl.py", line 273, in scan_once
    offset = int(cursor_raw) if cursor_raw is not None else 0
ValueError: invalid literal for int() with base 10: 'f18ed300-bc85-4b9f-918f-845d4bc5140c'
```

### It was never bad data

Against the live DB, every one of the 2,021 `jsonl` rows in `ingest_cursors` held a clean integer
byte offset, and the single `langsmith` row held an ISO timestamp. Nothing had ever written a UUID
there. But:

```sql
SELECT session_id, source FROM events WHERE session_id = 'f18ed300-bc85-4b9f-918f-845d4bc5140c';
-- f18ed300-bc85-4b9f-918f-845d4bc5140c | jsonl
```

The UUID is a **`session_id` from `events`** — the first row of
`SELECT DISTINCT session_id FROM events`. The read returned a *different query's* rows.

### Root cause

`EventWriter` owns exactly one `duckdb.DuckDBPyConnection` and one `threading.Lock`. **DuckDB parks
the pending result ON the connection object**, so `execute()` + `fetchone()` is a two-step,
non-atomic sequence. A second `execute()` from another thread in between makes the first caller's
`fetchone()` return the second query's rows.

`write` / `write_many` / `flush` / `snapshot_to` all took the lock. Two paths did not:

| Unlocked toucher of the shared connection | Thread |
|---|---|
| `store/writer.py:196` `get_cursor()` / `:202` `set_cursor()` | jsonl worker thread (`scan_once` via `asyncio.to_thread`, `jsonl.py:314`) |
| `ingest/langsmith_poll.py:80` `_known_session_ids()` — reached into `writer._conn` and ran `SELECT DISTINCT session_id FROM events` | event loop (`poll_once`) |

Both loops run concurrently (`cli.py:158-160`):

1. jsonl thread — `execute("SELECT cursor FROM ingest_cursors WHERE ...")`
2. event loop — `execute("SELECT DISTINCT session_id FROM events")` *replaces the pending result*
3. jsonl thread — `fetchone()` → gets a **session UUID**
4. `int('f18ed300-...')` → `ValueError`

Matches the original log exactly: the traceback lands immediately after two LangSmith HTTP 200s.

The comment at `langsmith_poll.py:76-78` shows the author knew DuckDB would refuse a second
connection and deliberately borrowed `writer._conn` — but missed that borrowing it needs
synchronisation. This is the **OQ-02 lesson** (already in CLAUDE.md: *"the whole `flush()` round-trip
is lock-protected, not just the buffer swap"*) applied to `flush()` and never to the cursors.

### Blast radius is wider than the crash

The crash is the *lucky* case. The same race can hand back a **valid but foreign byte offset**, or
`None` — silently rewinding a transcript's cursor to 0 or skipping data, with no error at all. The
reproduction below hit the `None` variant. Other latent consequences of the same shared connection:
`langsmith_poll.py:156` would `datetime.fromisoformat()` a byte offset, and `_flush_batch`'s
`_count_events()` `fetchone()` could pick up a stolen row.

### Reproduction (deterministic)

Two threads, one `EventWriter`, a scratch DB — one calling `_known_session_ids`, one calling
`get_cursor`. Fails in <2,000 iterations with the identical error shape. Now pinned as
`tests/unit/store/test_writer_concurrency.py`.

### Fix — two-sided; either half alone leaves the race open

- **`store/writer.py`** — `_conn` renamed to `__conn` (name-mangled: an outside `writer._conn` now
  raises `AttributeError` instead of silently working). `get_cursor` / `set_cursor` take `self._lock`
  for the whole execute+fetch round-trip.
  A plain `Lock` is still correct — no nesting exists: `write`/`write_many` release the lock *before*
  calling `flush()` (`:85-86`, `:94-95`), `_flush_batch` takes no lock, and `scan_once` calls
  `get_cursor` → `write_many` → `set_cursor` sequentially at top level. An `RLock` would be worse:
  it would *silently permit* the dangerous nesting rather than deadlocking loudly.
- **New `EventWriter.cursor()`** — the public read door. Returns an independent DuckDB cursor: its
  own result set, MVCC (committed rows only), never blocked by nor blocking an in-flight flush.
  `connect_read_only()` now uses it too (it had set the `_conn` precedent).
- **`ingest/langsmith_poll.py`** — `_known_session_ids` uses `writer.cursor()`.
  Deliberately **not** the write lock: it is a full-table `SELECT DISTINCT`, and putting that on the
  write lock would stall every OTLP/JSONL flush for the length of a table scan.
- **`ingest/jsonl.py`** — `_parse_offset()` tolerates a non-integer cursor (rescan from 0 + warn).
  **This is a guard, not the fix.** Merged *alone* it would have been actively harmful: it converts a
  loud crash into a silent, repeated full rescan while the corruption keeps happening elsewhere.

---

## P1 bugs found while testing the rest of the app

### 1. `correction_scan` had never fired once

`jobs/correction_scan.py:93` read `event["payload"]["text"]`. **0 of 4,522** `user_prompt` events in
the live DB have a top-level `text` key — from any source. The prompt text lives at
`payload.message.content`, which is a plain string (3,787×) or a content-block array (735×).

So `_has_correction_phrase` was never reached, `phrase_matches` was always 0, and the score was
driven by the reprompt heuristic alone — while the job cheerfully reported `status: ok`.

**Why the tests missed it:** the fixtures build `payload={"text": ...}`, a shape no ingest source has
ever produced. The tests encoded a fiction and passed against it.

Fixed with `_prompt_text()`, which handles both real shapes. After the fix, on real data:
**28 phrase matches across 599 sessions.**

### 2. Session detail rendered the literal string `None`

`sum(tokens_input)` over all-NULL rows is NULL, not 0 — **756 of 2,763** `v_tasks` rows — and
`partials/session_detail_fragment.html:19` dereferences it straight into the page. Every other view
already coalesces (`views.sql:46,81`); `v_tasks` did not. Fixed in the view.

### 3. OTLP 500'd on wrong-shaped-but-valid JSON

`parse_logs_payload`'s docstring claimed it was *"defensive throughout … never a crash."* It guarded
the **lists** but never the **element types**, and `_decode_any_value` did bare `int()`/`float()` on
exporter-supplied JSON. Confirmed with curl against a running receiver:

| body | before | after |
|---|---|---|
| `{"resourceLogs":["x"]}` | **500** | 400 |
| attribute `{"intValue":"abc"}` | **500** | 400 |

Bad *JSON* was already rejected with 400; bad *shape* crashed the handler. Fixed with
`MalformedPayloadError` + `_as_mapping()` at each boundary; the routes translate it to 400.

---

## Regression guards

The suite was green through all four of these, so the fixes ship with guards against that recurring:

- **`tests/unit/store/test_writer_concurrency.py`** — runs the two ingest loops *together*, the shape
  that actually broke. Asserts on the cursor's **value** (not `pytest.raises(ValueError)`), because
  the corruption can also return a foreign row or `None` — silent data loss, not a crash. Its
  `join(timeout=60)` doubles as a deadlock guard on the non-reentrant lock.
- **A structural test** that fails if *any* module outside `store/writer.py` contains `._conn`.
- **ruff `SLF`** (`pyproject.toml`) — private-member access is now a lint error repo-wide. Zero
  violations after the fix; `tests/**` is exempt.

---

## Verification (all run, all passing)

| # | Check | Result |
|---|---|---|
| 1 | `just check` | green — **265 passed** (was 256), lint/format/pyrefly/codespell clean |
| 2 | Real app, **fresh empty DB**, real `~/.claude/projects` + live LangSmith, both loops fast (5s scan / 15s poll), ~3 min unattended | **0** `scan pass failed`, **0** tracebacks, **0** ERROR lines, 2 LangSmith polls observed concurrently with the backfill |
| 3 | Data actually arrived (a silent no-op would also produce no errors) | 45,906 jsonl + 281 langsmith events |
| 4 | `SELECT count(*) FROM ingest_cursors WHERE source='jsonl' AND TRY_CAST(cursor AS BIGINT) IS NULL` | **0** of 935 |
| 5 | `v_tasks` NULL token sums | **0** of 1,200 (was 756 of 2,763) |
| 6 | `correction_scan` on real prompts | **28** phrase matches / 599 sessions (was 0, always) |
| 7 | OTLP shape-fuzz | both 500s → **400**; valid payload still 200 |
| 8 | Session detail page | **0** occurrences of `>None<` |

Reproducing #2 is the one that counts — it is the only check that would have caught the original
bug, and the only one that catches the silent-rewind variant.

---

## Known-remaining (P2, deferred — not firing today)

- **`web/app.py:80-87`** — the SSE loop calls `row[1].isoformat()` on a nullable `ts` with no guard,
  and `jsonl._parse_ts` can return `None`. There are 0 NULL-`ts` rows right now, but one bad
  transcript timestamp would permanently kill the live event stream. `jobs/correction_scan.py:72`
  sorts on the same nullable `ts` (`TypeError`).
- **`ingest/langsmith_poll.py:200`** — `run.start_time` may be `None` → `TypeError`, swallowed by
  `run_forever`'s blanket `except Exception`, stalling the poller at its old cursor.
- **`store/writer.py` `close()`** — calls `self.__conn.close()` *outside* the lock, so a concurrent
  `get_cursor`/`write` can land on a connection that is mid-close. A real shutdown race, though
  `bam serve` teardown makes it hard to hit.
- **`web/queries.py`** — six `assert row is not None` guards, which `python -O` strips.
