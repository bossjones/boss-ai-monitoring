"""Wire-level OTLP capture sink — for building REAL test fixtures (outstanding.md P2).

Listens on :4318, answers ``POST /v1/logs`` and ``POST /v1/metrics`` with ``200 {}``, and
appends every raw request body — pretty-printed, one JSON document per file — under
``captures/``. This is the same approach that produced
``tests/fixtures/otlp/real_session_logs.json``: capture the RAW wire JSON, never what the app
persisted (the app keeps columns + payload, not the OTLP envelope, and the fixture must be the
envelope).

Runbook (two terminals):

    # Terminal 1
    uv run python scripts/capture_otlp_fixture.py            # sink on :4318

    # Terminal 2 — telemetry env (same recipe as real_session_logs.json)
    export CLAUDE_CODE_ENABLE_TELEMETRY=1
    export OTEL_LOGS_EXPORTER=otlp
    export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    export OTEL_LOGS_EXPORT_INTERVAL=1000   # flush fast so short sessions export

    # (1) tool_decision with payload.source == 'config'  (feeds autonomy_score):
    #     in a scratch project, .claude/settings.json:  {"permissions": {"deny": ["Bash"]}}
    #     then:  claude -p "run: echo hello"            -> denial decided by config
    # (2) api_error  (feeds recovery_rate's error side):
    #     claude -p --model no-such-model "hi"          -> clean api_error, NO recovery
    # (3) api_error + recovery on the SAME prompt (the full recovery_rate signal) needs a
    #     TRANSIENT failure: start a multi-turn prompt, drop connectivity ~5s mid-task,
    #     restore, let the automatic retry finish the turn. This step is human-driven.

Sanitize before committing a capture as a fixture — identical recipe to
``real_session_logs.json`` (see tests/unit/ingest/test_otlp.py::TestRealCapturedSession):
user.id -> 64 zeros; session.id -> 11111111-2222-3333-4444-555555555555;
organization.id / user.account_uuid / user.account_id -> zero UUIDs;
user.email -> dev@example.com; plugin_id_hash -> zeros; prompt.id -> prompt-0001-style but
CONSISTENT across records (the metrics join on it). Verify prompt == "<REDACTED>" — if it is
not, the content-capture flags were ON: recapture, do not hand-redact. Never reorder
timeUnixNano / event.timestamp.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"


class _Sink(BaseHTTPRequestHandler):
    def _read_body(self) -> bytes:
        # The Claude Code exporter sends `Transfer-Encoding: chunked` (and sometimes gzip),
        # which BaseHTTPRequestHandler does NOT decode — a naive Content-Length read yields
        # zero bytes and the capture silently looks empty.
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().strip()
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF after the last-chunk marker
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after each chunk
            body = b"".join(chunks)
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body

    def do_POST(self) -> None:
        body = self._read_body()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%f")
        kind = self.path.strip("/").replace("/", "-") or "unknown"
        out = CAPTURE_DIR / f"otlp-{kind}-{stamp}.json"
        try:
            out.write_text(json.dumps(json.loads(body), indent=2) + "\n")
        except json.JSONDecodeError:
            out.with_suffix(".bin").write_bytes(body)
        print(f"captured {self.path} -> {out}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        pass  # the per-capture print above is the only output that matters


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wire-level OTLP capture sink (see module docstring for the runbook)"
    )
    parser.add_argument("--port", type=int, default=4318)
    args = parser.parse_args()
    CAPTURE_DIR.mkdir(exist_ok=True)
    print(f"OTLP capture sink on :{args.port}, writing to {CAPTURE_DIR}/ (Ctrl-C to stop)")
    HTTPServer(("127.0.0.1", args.port), _Sink).serve_forever()


if __name__ == "__main__":
    main()
