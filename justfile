# boss-ai-monitoring — `just check` is the definition of done.
# .github/workflows/ci.yml mirrors `check` EXACTLY; keep them in lockstep.

default:
    @just --list

# The full gate: lint, format, types (pyrefly — not mypy/ty), spelling, tests.
check: lint fmt-check typecheck spell test

lint:
    uv run ruff check .

fmt-check:
    uv run ruff format --check .

typecheck:
    uv run pyrefly check

spell:
    uv run codespell

test:
    uv run pytest -q

fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Local dev loop: ONE app, TWO binds (dashboard :8000 + OTLP :4318).
dev:
    uv run bam serve

# Packaging validation only — iterate with `just dev`, not by rebuilding the image.
docker-build:
    docker compose build

# Docker compose round-trip (build+up+curl+down). Slow, opt-in — not part of `check`.
test-docker:
    uv run pytest -m docker -v tests/integration/

# LangSmith VCR replay suite (hermetic — also runs as part of plain `just test`/`check`).
test-vcr:
    uv run pytest -m langsmith_vcr -v tests/integration/langsmith

# Re-record cassettes against the live LangSmith API (needs the direnv-ambient key),
# then prove the recordings are secret-free before they can be committed.
record-vcr:
    direnv exec . uv run pytest tests/integration/langsmith --vcr-mode=all -v
    uv run pytest tests/integration/langsmith/test_cassette_hygiene.py -v

db-path:
    @uv run bam config db-path
