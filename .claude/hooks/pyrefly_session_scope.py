#!/usr/bin/env python3
"""Stop hook: type-check only the Python files THIS session actually edited.

Why not the whole repo (the previous behavior, OQ-01): in a multi-agent run every pane shares one
working tree, so a repo-wide `pyrefly ... || exit 2` force-continues an IDLE pane over ANOTHER
pane's work-in-progress errors. A blocked agent then gets pressured into "helpfully" fixing files
it does not own — which breaks exclusive file ownership, the one invariant keeping a team run
free of merge conflicts.

Why not `git diff` either: same shared working tree, so pane A's WIP still shows up for pane B.
The scope has to be per-SESSION, and Claude Code hands us exactly that — `transcript_path` on
stdin, the session's own JSONL. We read the `Edit`/`Write`/`MultiEdit` tool calls out of it.

Full-tree pyrefly is still enforced by `just check`, `.pre-commit-config.yaml`, and `ci.yml`, so
nothing escapes the gate; this only stops the hook from blocking a session on other people's
in-flight work.

Fails OPEN (exit 0) on any problem of its own — a buggy hook must never wedge a session.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def edited_python_files(transcript_path: Path) -> list[Path]:
    """Every existing .py file this session wrote to, deduped, in a stable order."""
    found: dict[str, Path] = {}

    for line in transcript_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partial/garbage line is not a reason to block the session

        content = entry.get("message", {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in EDIT_TOOLS:
                continue
            raw = block.get("input", {}).get("file_path")
            if not isinstance(raw, str) or not raw.endswith(".py"):
                continue
            path = Path(raw)
            # Edited-then-deleted files are gone; pyrefly would just error on the missing path.
            if path.is_file():
                found[str(path)] = path

    return list(found.values())


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        transcript_path = Path(payload["transcript_path"])
        files = edited_python_files(transcript_path)
    except (json.JSONDecodeError, KeyError, OSError):
        return 0  # fail open: never wedge a session on a hook bug

    if not files:
        return 0

    project_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "uv",
            "run",
            "pyrefly",
            "check",
            "--baseline",
            "pyrefly-baseline.json",
            *[str(f) for f in files],
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return 2  # block the Stop: these are THIS session's own type errors
    return 0


if __name__ == "__main__":
    sys.exit(main())
