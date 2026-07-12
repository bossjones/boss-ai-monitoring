# 4. Troubleshooting

Symptom → cause → fix, for the failures that actually happen with this app.

## "No events arrive" — `/live` stays empty

This has four causes, roughly in order of likelihood.

### 1. Telemetry env vars aren't exported in the shell running Claude Code

`bam serve` only receives what Claude Code is told to send it. Confirm the four required
variables are actually set **in the terminal where you launch Claude Code** (not the terminal
running `bam serve` — exporting them there does nothing):

```bash
echo "$CLAUDE_CODE_ENABLE_TELEMETRY $OTEL_METRICS_EXPORTER $OTEL_LOGS_EXPORTER $OTEL_EXPORTER_OTLP_PROTOCOL $OTEL_EXPORTER_OTLP_ENDPOINT"
```

Expected: `1 otlp otlp http/json http://localhost:4318` (or `http://host.docker.internal:4318` for
Docker). If any of these print empty, `export` them per [tutorial 1](01-first-run.md) and start a
**new** Claude Code session — env vars are read at process startup.

### 2. Wrong protocol or endpoint

`OTEL_EXPORTER_OTLP_PROTOCOL` must be `http/json` — this receiver does not implement gRPC and
there's no otel-collector in front of it. If it's unset or `grpc`, Claude Code will silently fail
to export (or fail loudly, depending on version) rather than falling back.

Verify the receiver is actually listening and speaks JSON:

```bash
curl -i http://localhost:4318/v1/logs -X POST -H 'Content-Type: application/json' -d '{}'
```

Expected: `HTTP/1.1 200 OK` with body `{}`. A connection refused means nothing is listening on
`4318` at all — check that `bam serve` is actually running and that
`settings.server.otlp_port` / `otlp_bind` (`bam config show`) match what you expect.

### 3. Running in Docker on macOS and still using `localhost`

Covered in full in [tutorial 3](03-running-in-docker.md#step-2--the-macos-gotcha-pointing-telemetry-at-the-container).
Short version: a Claude Code session on the host generally cannot reach the container at
`localhost:4318` — use `OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318` instead.
This is the single most common reason for "no events arrive" once Docker is in the picture.

### 4. Everything above checks out, but you're looking at the wrong terminal's history

If you exported the variables *after* Claude Code was already running, that session won't pick
them up — telemetry export config is read once at startup. Start a fresh session.

## `IO Error: Could not set lock on file ...`

```
IO Error: Could not set lock on file "/path/to/bam.duckdb": Conflicting lock is held in
<python-path> (PID <pid>) by user <you>. See also https://duckdb.org/docs/stable/connect/concurrency
```

**Cause:** you tried to open the live DuckDB file directly (`duckdb "$(uv run bam config
db-path)" ...`, a fresh `marimo edit notebooks/explore.py` pointed at the live path, etc.) while
`bam serve` is running *and has written at least one event*. DuckDB's file lock is exclusive
across processes — `-readonly` does not get around it, because the lock is enforced regardless of
the mode the second process asks for.

**The trap:** the write connection is created *lazily*, only when the writer flushes its first
batch. So a `bam serve` process that just started, with no events ingested yet, holds **no lock** —
a direct query against the live file will appear to work right up until the first event lands,
then start failing with no code change on your end. Don't take "it worked a second ago" as proof
it's safe.

**Fix:** don't stop the app. Ask it for a snapshot instead — a consistent, point-in-time copy
(tables *and* views) that you're free to open directly:

```bash
duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
```

Same fix for marimo:

```bash
BAM_STORE__DB_PATH="$(uv run bam snapshot)" uvx marimo edit notebooks/explore.py
```

See [tutorial 2](02-exploring-your-data.md) for the full walkthrough.

## A query against `$BAM_DB_PATH` "succeeds" but returns nothing — and it's a lie

`BAM_DB_PATH` is a legacy flat alias for `store.db_path`; it is **not set by default in your
shell**, and referencing it directly is a real footgun, verified here directly:

```bash
$ unset BAM_DB_PATH
$ duckdb "$BAM_DB_PATH" "SELECT source, count(*) FROM events GROUP BY 1"
$ echo $?
0
```

No output, exit code `0` — looks like a clean, empty result. It is not. Because `$BAM_DB_PATH`
expands to an empty string, the `duckdb` CLI doesn't see a filename argument at all; it shifts the
SQL string itself into the filename slot and creates a **new, empty database file literally named
after your query text** in your current directory, then exits without ever running your query
(there was nothing left to run it against). You can see this happen:

```bash
$ duckdb "" "SELECT 42 AS answer"
$ ls "SELECT 42 AS answer"
-rw-r--r--  1 you  staff  12288 ... SELECT 42 AS answer
```

That stray file is a real, empty DuckDB database sitting in your working directory — clean it up
and don't mistake the silent exit-0 for a real (empty) result.

**Fix:** never reference `$BAM_DB_PATH` (or any bare env var) directly in a shell command. Always
resolve the path through the CLI, which prints the actual configured, absolute path:

```bash
duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"
```

The same applies to the notebook env var: it's `BAM_STORE__DB_PATH` (double underscore, nested
section — `BAM_` + `store` + `db_path`), not `BAM_DB_PATH`. Get the nesting wrong and you silently
fall back to whatever `store.db_path` resolves to from `config.yaml`/defaults instead of your
snapshot, with no error.

## LangSmith rows missing (`source: langsmith` never appears)

Two independent, non-error causes:

1. **No `LANGSMITH_API_KEY` set.** The poller degrades gracefully without one — the app still
   boots and serves, it just never has anything to poll with. Check for presence (never print the
   value):
   ```bash
   test -n "$LANGSMITH_API_KEY" && echo "set" || echo "unset"
   ```
2. **The 60-second poll hasn't fired yet.** The poller is cursor-based and rate-limit aware
   (`ingest.langsmith_poll_interval_s`, default 60s — LangSmith's own limit is roughly 10
   requests/10s, which is why it isn't tighter). Give it a full interval before concluding
   something's wrong.

If both of those check out and you still see nothing, confirm the two LangSmith project names
actually match — `CC_LANGSMITH_PROJECT` (what Claude Code traces *into*) and `LANGSMITH_PROJECT`
(what this app polls *from*) must name the **same** project, or you're tracing into one project
and reading back from another:

```bash
langsmith run list --project "$LANGSMITH_PROJECT" --limit 10
```

If that CLI call returns runs but the dashboard doesn't show them, it's a poll-timing or
project-name-mismatch issue, not a missing-key issue.

## `just check` is failing

`just check` (`lint fmt-check typecheck spell test`) is the project's definition of done — it's
what CI runs, exactly:

- **`lint`** — `uv run ruff check .`
- **`fmt-check`** — `uv run ruff format --check .` (use `just fmt` to auto-fix)
- **`typecheck`** — `uv run pyrefly check`. **The type checker is [pyrefly](https://pyrefly.org/),
  not mypy, not ty, not basedpyright.** Don't run or install those — pyrefly is what's configured
  and what CI enforces.
- **`spell`** — `uv run codespell`
- **`test`** — `uv run pytest -q`

Run the failing stage in isolation to see just that output, rather than the whole gate:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check
uv run codespell
uv run pytest -q
```

For a single failing test:

```bash
uv run pytest tests/unit/store/test_writer.py::test_name -q
```

## Quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `/live` stays empty | telemetry env vars not exported, wrong protocol/endpoint, or Docker+macOS `localhost` | export the 4 vars in Claude Code's shell; use `http/json`; use `host.docker.internal:4318` in Docker |
| `IO Error: Could not set lock ...` | queried the live DB while `bam serve` was writing | `duckdb "$(uv run bam snapshot)" ...` |
| Query "succeeds" with no output, exit 0 | bare `$BAM_DB_PATH` is unset, becomes the SQL-as-filename footgun | always use `"$(uv run bam config db-path)"` |
| `source: langsmith` never appears | no `LANGSMITH_API_KEY`, poll hasn't fired, or project name mismatch | check key presence, wait 60s, verify `CC_LANGSMITH_PROJECT` == `LANGSMITH_PROJECT` |
| `just check` fails | one of lint/format/typecheck/spell/test | run stages individually; typecheck is pyrefly, not mypy |
