# syntax=docker/dockerfile:1
#
# Packaging convenience only — `uv run bam serve` stays the primary dev loop. Fast-loop
# discipline (user directive): bring this image up the MINIMUM number of times, ideally once when
# it first works and once at GATE. Never rebuild to test a code change; iterate locally instead.

# ---- builder: resolve deps + install the project with uv --------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Deps first, cached separately from source changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now the project itself (src/ layout — htmx static file + Jinja templates ride along).
COPY src/ src/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim, non-root, nothing but the venv + source -----------------------------------
FROM python:3.13-slim-bookworm AS runtime

RUN useradd --create-home --uid 1000 bam
WORKDIR /app

COPY --from=builder --chown=bam:bam /app /app
ENV PATH="/app/.venv/bin:$PATH"

# /data is where compose.yaml mounts the named volume for the DuckDB file. A named volume's FIRST
# mount inherits whatever owner/perms already exist at that path in the image, so this has to be
# created (and owned by the non-root runtime user) here, before USER bam — otherwise the volume
# comes up root-owned and EventWriter's first CREATE TABLE fails with a permission error.
RUN mkdir -p /data && chown bam:bam /data

USER bam

# :8000 dashboard, :4318 OTLP http/json — the ONE FastAPI app, TWO binds (BL-02/cli.py).
EXPOSE 8000 4318

ENTRYPOINT ["bam"]
CMD ["serve"]
