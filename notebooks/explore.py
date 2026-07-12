import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium", app_title="boss-ai-monitoring explorer")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    mo.md(
        """
        # boss-ai-monitoring — ad-hoc exploration

        A **read-only** notebook (`store.connect_read_only()`) — it never opens a write connection
        (G5). Reach for this when a dashboard number needs digging into: "why did Tuesday cost
        $9?", a per-session breakdown, the token-class mix, or which tools keep failing.

        **Run this with `bam serve` stopped**, not alongside it: DuckDB takes an OS-level
        exclusive lock for the duration of any read-write connection, and that lock blocks *every
        other process's* connection to the same file — including a read-only one — regardless of
        which process opened it first. That's a DuckDB file-locking property, not a bug in this
        notebook or in `connect_read_only()`; it's a separate constraint from the in-process
        conflict tracked at OQ-04 (`.team/*.open-questions.md`), which is about `web`'s and
        `jobs`' readers sharing `bam serve`'s own OS process, not about a standalone tool like
        this one. If you need to explore while the app keeps running, work off a copy of the
        DuckDB file instead.

        Run modes: `uvx marimo edit notebooks/explore.py` (interactive, editable) or
        `uvx marimo run notebooks/explore.py` (read-only app view). See
        `specs/boss-ai-monitoring/boss-ai-monitoring.html` for the full spec.
        """
    )
    return


@app.cell
def _():
    from boss_ai_monitoring.config import load_settings
    from boss_ai_monitoring.store.writer import connect_read_only

    settings = load_settings()
    db_path = settings.store.db_path
    return connect_read_only, db_path


@app.cell
def _(connect_read_only, db_path, mo):
    mo.stop(
        not db_path.exists(),
        mo.md(
            f"No DuckDB file at `{db_path}` yet. Run `bam serve`, let a Claude Code session or two "
            "post some telemetry, then re-run this notebook."
        ),
    )
    conn = connect_read_only(db_path)
    return (conn,)


@app.cell
def _(conn):
    def rows(sql: str, params: list | None = None) -> list[dict]:
        """One query -> a list of plain dicts. No pandas/polars/pyarrow dependency (none of
        those are in this project's dependency set — `mo.ui.table` accepts plain dicts directly)."""
        cursor = conn.execute(sql, params or [])
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    return (rows,)


@app.cell
def _(mo):
    mo.md('## Per-day cost drill-down — "why did Tuesday cost $9?"')
    return


@app.cell
def _(mo, rows):
    daily = rows("SELECT * FROM v_costs_daily ORDER BY day DESC")
    mo.ui.table(daily, label="Daily cost + event count (v_costs_daily)")
    return (daily,)


@app.cell
def _(daily, mo):
    day_options = [str(row["day"]) for row in daily]
    day_picker = mo.ui.dropdown(
        day_options,
        value=day_options[0] if day_options else None,
        label="Day to drill into",
    )
    day_picker
    return (day_picker,)


@app.cell
def _(day_picker, mo, rows):
    mo.stop(day_picker.value is None, mo.md("No days with cost data yet."))
    day_sessions = rows(
        "SELECT session_id, started_at, ended_at, duration_ms, cost_usd, model "
        "FROM v_sessions WHERE CAST(started_at AS DATE) = CAST(? AS DATE) ORDER BY started_at",
        [day_picker.value],
    )
    mo.ui.table(day_sessions, label=f"Sessions on {day_picker.value}")
    return


@app.cell
def _(mo):
    mo.md("## Per-session breakdown")
    return


@app.cell
def _(mo, rows):
    sessions = rows("SELECT * FROM v_sessions ORDER BY started_at DESC LIMIT 200")
    mo.ui.table(sessions, label="Most recent 200 sessions (v_sessions)")
    return


@app.cell
def _(mo):
    mo.md("## Token-class mix")
    return


@app.cell
def _(mo, rows):
    token_mix = rows(
        """
        SELECT
            sum(tokens_input) AS tokens_input,
            sum(tokens_output) AS tokens_output,
            sum(tokens_cache_read) AS tokens_cache_read,
            sum(tokens_cache_creation) AS tokens_cache_creation
        FROM events
        """
    )
    mo.ui.table(token_mix, label="Token totals across all events")
    return


@app.cell
def _(mo):
    mo.md("## Tool failure explorer")
    return


@app.cell
def _(mo, rows):
    tool_failures = rows(
        "SELECT tool_name, call_count, success_count, success_rate, p50_duration_ms, p95_duration_ms "
        "FROM v_tool_stats WHERE success_rate < 1.0 ORDER BY call_count DESC"
    )
    mo.ui.table(tool_failures, label="Tools with at least one failure (v_tool_stats)")
    return


if __name__ == "__main__":
    app.run()
