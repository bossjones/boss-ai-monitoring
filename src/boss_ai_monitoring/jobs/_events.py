"""Local stub of the canonical event envelope (BL-01, .team/*.backlog.md).

Wave 1 is hermetic: store hasn't published a concrete module yet, so jobs does not import from
it. This alias exists only so job functions have a name for "one row of the events table" — it is
NOT a new source of truth. Once store lands, callers still just pass dicts shaped like this; no
import here changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

Event = Mapping[str, Any]


def ordered_by_ts(events: Iterable[Event]) -> list[Event]:
    """Events oldest-first, DROPPING any row with no ``ts``.

    THE rule for every time-ordered job. `ts` is nullable and genuinely arrives NULL — a transcript
    line with a malformed `timestamp` makes `jsonl._parse_ts` return None — so sorting on it raised
    `TypeError: '<' not supported between 'NoneType' and 'datetime'` and the scheduler turned that
    into a permanent `correction_scan: error` badge.

    Drop rather than sort-NULLs-last: a ts-less event cannot be *differenced* either (the reprompt
    window subtracts two timestamps), so tolerating it in the sort only moves the crash. And drop
    rather than treat-as-epoch: a fake 1970 timestamp would silently push every later prompt
    outside the reprompt window. `drift_check` already applies this rule inline.
    """
    return sorted((e for e in events if e.get("ts") is not None), key=lambda e: e["ts"])
