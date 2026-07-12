"""VCR harness for the LangSmith integration suite.

Adapted from langsmith-sdk's python/tests/integration_tests/conftest.py with the host
allowlist inverted: they record only AI-provider calls and hit LangSmith live; we record
ONLY api.smith.langchain.com and drop everything else. Two record modes matter:

- --vcr-mode=none (default): replay-only. LANGSMITH_API_KEY is monkeypatched to a fake so
  the poller's presence check passes, and any request a cassette can't answer is a loud
  vcrpy error — the suite is hermetic (no network, no secrets, runs in `just check`/CI).
- --vcr-mode=all (via `just record-vcr`): re-records against the live API using the
  direnv-ambient key; skips if the key is absent (presence check only, never echoed).

Cassettes are scrubbed BEFORE hitting disk (auth headers replaced with FILTERED, body
regexes for lsv2_/Bearer/api_key tokens, tenant UUIDs normalized, Set-Cookie stripped) and
recorded with decode_compressed_response=True so every body stays grep-auditable —
test_cassette_hygiene.py enforces all of this on every run.

Privacy note: cassettes still contain run *metadata* (run names, model names, token
counts). The poller's _SELECT_FIELDS deliberately excludes inputs/outputs, so no prompt or
completion content can ever appear in a recording — the select-list is a privacy boundary,
not just a bandwidth optimization (repo stance: content capture stays OFF).
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import vcr
from vcr.record_mode import RecordMode

CASSETTE_DIR = Path(__file__).parent / "cassettes"
ALLOWED_HOST = "api.smith.langchain.com"
FAKE_API_KEY = "lsv2_pt_vcr-fake-key"
FIXED_TENANT_UUID = "00000000-0000-4000-8000-000000000000"
FIXED_USER_HASH = "0" * 64

_LSV2_KEY = re.compile(r"lsv2_[A-Za-z0-9_]+")
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9_-]+")
_JSON_API_KEY = re.compile(r'(api[-_]?key"?\s*:\s*")\w+(")')
# The workspace/tenant UUID leaks through three shapes: the /sessions "tenant_id" field,
# the LANGSMITH_WORKSPACE_ID run-metadata key, and the /o/<org>/ segment of app_path.
_TENANT_ID = re.compile(r'("tenant_id"\s*:\s*")[0-9a-fA-F-]+(")')
_WORKSPACE_ID = re.compile(r'("LANGSMITH_WORKSPACE_ID"\s*:\s*")[0-9a-fA-F-]+(")')
_ORG_PATH = re.compile(r"(/o/)[0-9a-fA-F-]+")
# Hashed account identifiers in run metadata (anthropic_user_id / user_id).
_USER_HASH = re.compile(r'("(?:anthropic_)?user_id"\s*:\s*")[0-9a-fA-F]{16,}(")')


def _scrub_body(body: str) -> str:
    body = _LSV2_KEY.sub("lsv2_FILTERED", body)
    body = _BEARER.sub("FILTERED", body)
    body = _JSON_API_KEY.sub(r"\1FILTERED\2", body)
    body = _TENANT_ID.sub(rf"\g<1>{FIXED_TENANT_UUID}\g<2>", body)
    body = _WORKSPACE_ID.sub(rf"\g<1>{FIXED_TENANT_UUID}\g<2>", body)
    body = _ORG_PATH.sub(rf"\g<1>{FIXED_TENANT_UUID}", body)
    return _USER_HASH.sub(rf"\g<1>{FIXED_USER_HASH}\g<2>", body)


def _filter_request(request: Any) -> Any:
    """Drop any request not aimed at the LangSmith API; scrub bodies of the rest."""
    if request.host != ALLOWED_HOST:
        return None
    if request.body:
        try:
            raw = request.body
            body = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            scrubbed = _scrub_body(body)
            request.body = scrubbed.encode("utf-8") if isinstance(raw, bytes) else scrubbed
        except UnicodeDecodeError:
            pass
    return request


def _filter_response(response: dict[str, Any]) -> dict[str, Any]:
    """Strip Set-Cookie, scrub body text, and normalize tenant UUIDs before writing."""
    headers = response.get("headers", {})
    for name in [n for n in headers if n.lower() == "set-cookie"]:
        del headers[name]
    body = response.get("body", {})
    if isinstance(body.get("string"), bytes):
        with contextlib.suppress(UnicodeDecodeError):
            body["string"] = _scrub_body(body["string"].decode("utf-8")).encode("utf-8")
    elif isinstance(body.get("string"), str):
        body["string"] = _scrub_body(body["string"])
    return response


def _create_vcr_instance(record_mode: str) -> vcr.VCR:
    return vcr.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode=RecordMode(record_mode),
        # vcrpy default match_on (no body): resilient to harmless SDK body-key reordering;
        # sequential same-URI POSTs (pagination) replay in recorded order.
        filter_headers=[
            ("x-api-key", "FILTERED"),
            ("X-Api-Key", "FILTERED"),
            ("Authorization", "FILTERED"),
            ("User-Agent", "FILTERED"),
            ("x-tenant-id", FIXED_TENANT_UUID),
        ],
        before_record_request=_filter_request,
        before_record_response=_filter_response,
        # Non-negotiable: an undecoded gzip body serializes as !!binary — unauditable.
        decode_compressed_response=True,
    )


@pytest.fixture(autouse=True)
def langsmith_auth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake key for hermetic replay; presence-check (never echo) the real key to record."""
    if request.node.get_closest_marker("langsmith_vcr") is None:
        return
    if str(request.config.getoption("--vcr-mode")) == "none":
        monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
        for var in ("LANGSMITH_API_URL", "LANGSMITH_ENDPOINT"):
            monkeypatch.delenv(var, raising=False)
    elif not os.environ.get("LANGSMITH_API_KEY"):
        pytest.skip("recording needs the direnv-ambient LANGSMITH_API_KEY")


@pytest.fixture(autouse=True)
def vcr_cassette(request: pytest.FixtureRequest) -> Iterator[None]:
    """Wrap each marked test in a {module}_{function}.yaml cassette (SDK naming scheme)."""
    if request.node.get_closest_marker("langsmith_vcr") is None:
        yield
        return
    record_mode = str(request.config.getoption("--vcr-mode"))
    module_name = request.module.__name__.split(".")[-1]
    cassette_name = f"{module_name}_{request.function.__name__}.yaml"
    with _create_vcr_instance(record_mode).use_cassette(cassette_name):
        yield
