"""Secret-scan guard over the committed LangSmith VCR cassettes.

Deliberately unmarked: it runs in every suite invocation (milliseconds of file I/O), so a
cassette that somehow lands unscrubbed fails CI even when the langsmith_vcr suite is
deselected. Passes vacuously while the cassette dir is empty.

The scrubbing contract enforced here (produced by tests/integration/langsmith/conftest.py):
- no LangSmith API keys (`lsv2_...`) other than the FILTERED/fake-replay placeholders
- no bearer tokens
- no `x-api-key` header values other than the FILTERED placeholder
- no `!!binary` bodies — an undecoded (gzip) body cannot be audited by grep or a reviewer,
  so `decode_compressed_response=True` is non-negotiable at record time
- every workspace/tenant identifier (tenant_id, LANGSMITH_WORKSPACE_ID, the /o/<org>/
  app_path segment) normalized to the fixed placeholder UUID, and every hashed account id
  (anthropic_user_id / user_id) normalized to the fixed zero hash
- never the literal ambient LANGSMITH_API_KEY value (compared in memory, never printed)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

CASSETTE_DIR = Path(__file__).parent / "cassettes"
FIXED_TENANT_UUID = "00000000-0000-4000-8000-000000000000"
FIXED_USER_HASH = "0" * 64

_LSV2_KEY = re.compile(r"lsv2_(?!FILTERED|pt_vcr-fake)[A-Za-z0-9_]+")
_BEARER_TOKEN = re.compile(r"Bearer [A-Za-z0-9]")
_TENANT_VALUES = re.compile(
    r'"(?:tenant_id|LANGSMITH_WORKSPACE_ID)"\s*:\s*"([0-9a-fA-F-]+)"|/o/([0-9a-fA-F-]+)'
)
_USER_HASH_VALUES = re.compile(r'"(?:anthropic_)?user_id"\s*:\s*"([0-9a-fA-F]+)"')
_YAML_LIST_ITEM = re.compile(r"^\s*-\s+(.*)$")


def _cassettes() -> list[Path]:
    return sorted(CASSETTE_DIR.glob("*.yaml"))


def _header_values(text: str, name: str) -> list[str]:
    """Collect a header's values in both YAML forms: same-line and `- item` list."""
    header_re = re.compile(rf"^\s*{re.escape(name)}:\s*(.*)$", re.IGNORECASE)
    lines = text.splitlines()
    values: list[str] = []
    for i, line in enumerate(lines):
        header = header_re.match(line)
        if not header:
            continue
        if inline := header.group(1).strip():
            values.append(inline)
        for follower in lines[i + 1 :]:
            item = _YAML_LIST_ITEM.match(follower)
            if not item:
                break
            values.append(item.group(1).strip())
    return values


def test_cassettes_contain_no_secrets() -> None:
    ambient_key = os.environ.get("LANGSMITH_API_KEY", "")
    for cassette in _cassettes():
        text = cassette.read_text()
        assert not _LSV2_KEY.search(text), f"{cassette.name}: unscrubbed lsv2_ API key"
        assert not _BEARER_TOKEN.search(text), f"{cassette.name}: unscrubbed bearer token"
        for value in _header_values(text, "x-api-key"):
            assert "FILTERED" in value, (
                f"{cassette.name}: x-api-key header with a non-FILTERED value"
            )
        for value in _header_values(text, "x-tenant-id"):
            assert value == FIXED_TENANT_UUID, f"{cassette.name}: un-normalized x-tenant-id header"
        assert "!!binary" not in text, (
            f"{cassette.name}: undecoded (gzip) body — record with decode_compressed_response"
        )
        for match in _TENANT_VALUES.finditer(text):
            value = match.group(1) or match.group(2)
            assert value == FIXED_TENANT_UUID, (
                f"{cassette.name}: un-normalized workspace/tenant identifier"
            )
        for match in _USER_HASH_VALUES.finditer(text):
            assert match.group(1) == FIXED_USER_HASH, (
                f"{cassette.name}: un-normalized account id hash"
            )
        if ambient_key:
            assert ambient_key not in text, f"{cassette.name}: contains the ambient API key"
