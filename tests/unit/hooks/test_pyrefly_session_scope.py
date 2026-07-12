"""The Stop hook must type-check only THIS session's own edits.

Why this exists (OQ-01): the previous hook ran `pyrefly` over the whole repo and `exit 2`'d. In a
multi-agent run all panes share one working tree, so an IDLE pane got force-continued over ANOTHER
pane's work-in-progress errors — which pressures a blocked agent into editing files it does not
own, breaking the one invariant that keeps a team run conflict-free. Scoping by `git diff` would
NOT fix that (same shared tree); scoping by the session's own transcript does.

Full-tree pyrefly is still enforced by `just check`, pre-commit, and CI — nothing escapes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[3]
HOOK = PROJECT_DIR / ".claude" / "hooks" / "pyrefly_session_scope.py"


@pytest.fixture
def in_repo_py() -> Iterator[Callable[[str, str], Path]]:
    """Write a throwaway .py file INSIDE the repo.

    It has to live in-tree: pyrefly resolves its config per-file, and on a path outside the
    project it silently falls back to a `basic` preset and reports 0 errors — so a test using
    tmp_path would pass while proving nothing.
    """
    scratch = PROJECT_DIR / ".hooktest"
    scratch.mkdir(exist_ok=True)
    created: list[Path] = []

    def _write(name: str, source: str) -> Path:
        path = scratch / name
        path.write_text(source)
        created.append(path)
        return path

    yield _write

    for path in created:
        path.unlink(missing_ok=True)
    scratch.rmdir()


def _run(hook_input: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        check=False,
    )


def _transcript(tmp_path: Path, edited: list[str]) -> Path:
    """A minimal Claude Code transcript: assistant turns with Edit/Write tool_use blocks."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]
                },
            }
        )
        for path in edited
    ]
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


def test_exits_zero_when_the_session_touched_no_python(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, [str(tmp_path / "README.md")])

    result = _run({"transcript_path": str(transcript)})

    assert result.returncode == 0


def test_exits_two_on_a_type_error_in_a_file_this_session_edited(
    tmp_path: Path, in_repo_py: Callable[[str, str], Path]
) -> None:
    bad = in_repo_py("bad.py", "def f() -> int:\n    return 'not an int'\n")
    transcript = _transcript(tmp_path, [str(bad)])

    result = _run({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "bad.py" in result.stderr


def test_ignores_files_this_session_did_not_touch(
    tmp_path: Path, in_repo_py: Callable[[str, str], Path]
) -> None:
    """THE multi-agent case: another pane's broken WIP file must not block this session.

    Both files are in-tree and both are broken — the ONLY thing keeping the hook quiet is that
    the transcript names just one of them. This is the test that would have caught OQ-01.
    """
    in_repo_py("someone_elses.py", "def f() -> int:\n    return 'boom'\n")
    mine = in_repo_py("mine.py", "def g() -> int:\n    return 1\n")
    transcript = _transcript(tmp_path, [str(mine)])

    result = _run({"transcript_path": str(transcript)})

    assert result.returncode == 0
    assert "someone_elses.py" not in result.stderr


def test_exits_zero_when_the_transcript_is_missing_or_unparseable(tmp_path: Path) -> None:
    """Never wedge a session on a hook bug — fail open, `just check` is still the real gate."""
    assert _run({"transcript_path": str(tmp_path / "nope.jsonl")}).returncode == 0
    assert _run({}).returncode == 0

    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json at all\n{partial\n")
    assert _run({"transcript_path": str(garbage)}).returncode == 0


def test_skips_files_that_no_longer_exist(tmp_path: Path) -> None:
    """A file edited then deleted during the session must not crash the hook."""
    transcript = _transcript(tmp_path, [str(tmp_path / "deleted.py")])

    assert _run({"transcript_path": str(transcript)}).returncode == 0
