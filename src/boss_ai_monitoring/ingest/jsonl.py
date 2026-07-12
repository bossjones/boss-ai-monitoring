"""Incremental JSONL transcript reader (Phase 4): historical backfill + gap-fill for the OTLP
live stream, and the safety net for whatever happened while the OTLP endpoint was down.

``~/.claude/projects/**/*.jsonl`` is a reverse-engineered format that shifts across Claude Code
versions (spec RISK #1, shared.md) — there is no published schema. Every parse decision here is
best-effort and was derived by inspecting real transcripts; see
``.team/boss-ai-monitoring-build.open-questions.md`` (OQ-jsonl-01) for exactly what was assumed.
Unknown fields always land in ``payload``; a malformed line is skipped with a logged warning,
never a crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from boss_ai_monitoring.store.schema import EVENT_COLUMNS

if TYPE_CHECKING:
    from boss_ai_monitoring.store.writer import EventWriter

logger = logging.getLogger(__name__)

Event = dict[str, Any]

SOURCE = "jsonl"
DEFAULT_SCAN_GLOB = "**/*.jsonl"

# Plausible field names for a per-line git SHA, checked defensively. Not one of these has ever
# been observed in a real transcript (see OQ-jsonl-01) — extraction stays best-effort so a future
# Claude Code version that *does* start emitting one is picked up with no code change here.
_GIT_SHA_KEYS = ("gitSha", "git_sha", "gitCommit", "commitSha")


def _event_defaults() -> Event:
    return dict.fromkeys(EVENT_COLUMNS)


def _make_event(**overrides: Any) -> Event:
    event = _event_defaults()
    event.update(overrides)
    event["source"] = SOURCE
    return event


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_git_sha(entry: dict[str, Any]) -> str | None:
    for key in _GIT_SHA_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass
class _SessionState:
    """Carried across lines of one session_id within a single scan pass.

    Assistant lines never repeat ``promptId`` in real transcripts — only the user-authored lines
    (the initiating prompt and each tool_result) do. So the active prompt_id is threaded forward:
    the most recently seen ``promptId`` on a user-type line applies to every line after it, until
    the next one. This is the best-effort heuristic named in OQ-jsonl-01.
    """

    prompt_id: str | None = None
    cwd: str | None = None
    git_sha: str | None = None
    pending_tool_use: dict[str, str | None] = field(default_factory=dict)


def parse_line(raw_line: str, state: dict[str, _SessionState]) -> list[Event]:
    """Parse one JSONL line into zero or more Events. Never raises.

    Malformed JSON, unrecognized entry types, and pure book-keeping lines (mode/attachment/
    system/etc.) all return ``[]`` — only lines that map to a meaningful observability event
    produce one.
    """
    stripped = raw_line.strip()
    if not stripped:
        return []

    try:
        entry = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("jsonl: skipping malformed line (%d bytes)", len(stripped))
        return []

    if not isinstance(entry, dict):
        return []

    entry_type = entry.get("type")
    session_id = entry.get("sessionId")
    ts = _parse_ts(entry.get("timestamp"))
    uuid = entry.get("uuid")

    sess = state.setdefault(session_id, _SessionState()) if session_id else _SessionState()

    cwd = entry.get("cwd")
    if isinstance(cwd, str) and cwd:
        sess.cwd = cwd
    git_sha = _extract_git_sha(entry)
    if git_sha:
        sess.git_sha = git_sha

    if entry_type == "assistant":
        return _parse_assistant(entry, sess, session_id=session_id, ts=ts, uuid=uuid)
    if entry_type == "user":
        return _parse_user(entry, sess, session_id=session_id, ts=ts, uuid=uuid)
    return []


def _parse_assistant(
    entry: dict[str, Any],
    sess: _SessionState,
    *,
    session_id: Any,
    ts: datetime | None,
    uuid: Any,
) -> list[Event]:
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    content = content if isinstance(content, list) else []

    # Track tool_use blocks so a later tool_result (matched by tool_use_id) can carry tool_name.
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_use_id = block.get("id")
            if tool_use_id:
                sess.pending_tool_use[tool_use_id] = block.get("name")

    usage = message.get("usage")
    if not isinstance(usage, dict):
        return []

    request_id = entry.get("requestId")
    event_id = f"jsonl:{uuid}:api_request" if uuid else f"jsonl:{session_id}:{request_id}"
    return [
        _make_event(
            event_id=event_id,
            ts=ts,
            event_type="api_request",
            session_id=session_id,
            prompt_id=sess.prompt_id,
            request_id=request_id,
            model=message.get("model"),
            git_sha=sess.git_sha,
            cwd=sess.cwd,
            tokens_input=usage.get("input_tokens"),
            tokens_output=usage.get("output_tokens"),
            tokens_cache_read=usage.get("cache_read_input_tokens"),
            tokens_cache_creation=usage.get("cache_creation_input_tokens"),
            payload=entry,
        )
    ]


def _parse_user(
    entry: dict[str, Any],
    sess: _SessionState,
    *,
    session_id: Any,
    ts: datetime | None,
    uuid: Any,
) -> list[Event]:
    prompt_id = entry.get("promptId")
    if isinstance(prompt_id, str) and prompt_id:
        sess.prompt_id = prompt_id

    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")

    tool_result_blocks = [
        block
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]

    if tool_result_blocks:
        events: list[Event] = []
        for i, block in enumerate(tool_result_blocks):
            tool_use_id = block.get("tool_use_id")
            tool_name = sess.pending_tool_use.pop(tool_use_id, None) if tool_use_id else None
            event_id = f"jsonl:{uuid}:tool_result:{i}" if uuid else f"jsonl:{session_id}:tr:{i}"
            events.append(
                _make_event(
                    event_id=event_id,
                    ts=ts,
                    event_type="tool_result",
                    session_id=session_id,
                    prompt_id=sess.prompt_id,
                    tool_name=tool_name,
                    success=not bool(block.get("is_error")),
                    git_sha=sess.git_sha,
                    cwd=sess.cwd,
                    payload=entry,
                )
            )
        return events

    if entry.get("isCompactSummary"):
        event_id = f"jsonl:{uuid}:compaction" if uuid else f"jsonl:{session_id}:compaction"
        return [
            _make_event(
                event_id=event_id,
                ts=ts,
                event_type="compaction",
                session_id=session_id,
                prompt_id=sess.prompt_id,
                git_sha=sess.git_sha,
                cwd=sess.cwd,
                payload=entry,
            )
        ]

    # a genuine human turn: content is either a plain string or a list of content blocks with
    # no tool_result among them (attachments, plan text, etc. all still count as the prompt).
    if content:
        event_id = f"jsonl:{uuid}:user_prompt" if uuid else f"jsonl:{session_id}:up"
        return [
            _make_event(
                event_id=event_id,
                ts=ts,
                event_type="user_prompt",
                session_id=session_id,
                prompt_id=sess.prompt_id,
                git_sha=sess.git_sha,
                cwd=sess.cwd,
                payload=entry,
            )
        ]

    return []


@dataclass
class ScanStats:
    files_scanned: int = 0
    events_written: int = 0


def scan_once(projects_dir: Path, writer: EventWriter) -> ScanStats:
    """One incremental pass over the transcript directory.

    Each ``*.jsonl`` file is read from its persisted byte-offset cursor forward. A file that
    shrank since the last cursor (truncated or rotated in place) restarts from byte 0 rather than
    seeking past the end. The directory itself is never written to.
    """
    stats = ScanStats()
    if not projects_dir.is_dir():
        return stats

    session_states: dict[str, _SessionState] = {}
    for path in sorted(projects_dir.glob(DEFAULT_SCAN_GLOB)):
        stats.files_scanned += 1
        cursor_key = str(path)
        cursor_raw = writer.get_cursor(SOURCE, cursor_key)
        offset = int(cursor_raw) if cursor_raw is not None else 0

        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < offset:
            offset = 0  # truncated or rotated — restart this file from scratch

        events: list[Event] = []
        with path.open(encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for raw_line in f:
                events.extend(parse_line(raw_line, session_states))
            new_offset = f.tell()

        if events:
            writer.write_many(events)
            stats.events_written += len(events)
        writer.set_cursor(SOURCE, cursor_key, str(new_offset))

    return stats


async def run_forever(
    projects_dir: Path,
    writer: EventWriter,
    *,
    interval_s: float,
    iterations: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Poll `scan_once` on a fixed interval. `iterations=None` loops forever; a finite count is
    for tests — the sleep never runs after the final pass."""
    count = 0
    while iterations is None or count < iterations:
        try:
            # OFF THE EVENT LOOP. `scan_once` is synchronous and, over a real ~/.claude/projects,
            # takes minutes on the first backfill — awaiting it directly freezes the whole app
            # (dashboard, OTLP receiver, SSE) for the duration. The EventWriter serializes its own
            # writes behind a lock, so it is safe to call from a worker thread.
            await asyncio.to_thread(scan_once, projects_dir, writer)
        except Exception:
            logger.exception("jsonl: scan pass failed, will retry next interval")
        count += 1
        if iterations is not None and count >= iterations:
            break
        await sleep(interval_s)
