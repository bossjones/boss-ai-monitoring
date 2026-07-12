"""Local stub of the canonical event envelope (BL-01, .team/*.backlog.md).

Wave 1 is hermetic: store hasn't published a concrete module yet, so jobs does not import from
it. This alias exists only so job functions have a name for "one row of the events table" — it is
NOT a new source of truth. Once store lands, callers still just pass dicts shaped like this; no
import here changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

Event = Mapping[str, Any]
