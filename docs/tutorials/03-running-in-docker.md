# 3. Running in Docker

## What you'll do

Bring the app up as a container with `docker compose`, and connect a Claude Code session running
on your host machine to it — including the macOS networking gotcha that is the single most common
reason "no events arrive" in this setup. ~10 minutes.

## Prerequisites

- Docker Desktop (macOS) or Docker Engine (Linux) with Compose v2 (`docker compose`, not the
  standalone `docker-compose`).
- This repo cloned locally.

## What's in `compose.yaml`

One service (`app`), built from the repo's `Dockerfile`:

- Ports: `8000` (dashboard) and `4318` (OTLP receiver) published to the host.
- A named volume (`bam_data`, mounted at `/data`) for the DuckDB file — `BAM_STORE__DB_PATH` is
  set to `/data/bam.duckdb` inside the container, so the database survives container restarts.
- A **read-only** bind mount of `${HOME}/.claude/projects` to `/claude-projects` inside the
  container — this is your real Claude Code history, never mounted writable.
- `BAM_SERVER__DASHBOARD_BIND` / `BAM_SERVER__OTLP_BIND` set to `0.0.0.0` — the local-dev default
  of `127.0.0.1` (see `config.sample.yaml`) would make the published ports unreachable from the
  host, since Docker's port forwarding lands on the container's external interface, not loopback.
- LangSmith variables (`LANGSMITH_API_KEY`, `TRACE_TO_LANGSMITH`, etc.) pass through from your
  shell/`.env` if set, and are absent otherwise — same graceful-degradation behavior as running
  locally.

## Step 1 — bring it up

```bash
docker compose up --build
```

`--build` is only needed the first time (or after a source change you intend to test in a
container). This project's fast-loop discipline is: iterate against `uv run bam serve` locally
(tutorial 1), and only bring `docker compose` up sparingly — packaging validation, not your normal
edit loop. Expect the first build to take a minute or two (dependency resolution + `uv sync`
inside the builder stage); subsequent builds are cached and much faster.

Expected output tail, once it's up:

```
app-1  | INFO bam: serving dashboard on 0.0.0.0:8000 and OTLP on 0.0.0.0:4318
app-1  | INFO:     Uvicorn running on http://0.0.0.0:8000
app-1  | INFO:     Uvicorn running on http://0.0.0.0:4318
```

Open [http://localhost:8000](http://localhost:8000) from the host — this part works exactly like
the local run, because the dashboard port is a normal published port and your browser is a normal
host-side client.

## Step 2 — the macOS gotcha: pointing telemetry at the container

This is the part that trips people up, and it's called out directly in `compose.yaml`'s comments
as **the #1 reason "no events arrive"** when running containerized: `localhost:4318` only reaches
the container from a process that is on the literal same network namespace as the published port.
That's true of a plain terminal on the Mac itself, but not of every place Claude Code might
actually be running — a devcontainer, a separate sandboxed process, or (on a VM-backed Docker
Desktop setup) any environment where `localhost` isn't 1:1 with the Mac host. Copy-pasting the
`.env.sample` value (`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`) into one of those is the
failure mode people hit.

The reliable fix, and the one to reach for by default whenever Claude Code and the container
aren't provably in the same network namespace:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318
```

`host.docker.internal` is a DNS name Docker Desktop provides automatically that always resolves to
the host machine, regardless of which network namespace the caller is in. `compose.yaml` also adds
an `extra_hosts: host.docker.internal:host-gateway` entry, which is a no-op on Docker Desktop
(already provided) but is what makes the same variable work on Linux Docker Engine 20.10+.

Full variable set for a host-side Claude Code session talking to the containerized app:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318
```

Use Claude Code, then check `/live` on the dashboard exactly as in
[tutorial 1](01-first-run.md). If you still see nothing, `curl` the endpoint directly to isolate
networking from telemetry config:

```bash
curl -i http://host.docker.internal:4318/v1/logs -X POST -H 'Content-Type: application/json' -d '{}'
```

A `200 {}` response means the network path is fine and the problem is upstream (Claude Code's
telemetry env vars); a connection error means the container isn't reachable by that name from
wherever you ran `curl`.

## Step 3 — JSONL backfill in the container

The container mounts `~/.claude/projects` read-only at `/claude-projects`, and
`BAM_INGEST__CLAUDE_PROJECTS_DIR` is set to that container path — so JSONL backfill works exactly
as it does locally, scanning your real transcript history, without you doing anything extra.

## Step 4 — checking the data in a running container

The same DuckDB locking rule from [tutorial 2](02-exploring-your-data.md) applies here — the
running container holds the write lock on `/data/bam.duckdb` inside its volume. Ask the
containerized app for a snapshot the same way, just against the published port:

```bash
curl -s -X POST http://localhost:8000/api/snapshot
```

This returns JSON with the snapshot's path *inside the container's filesystem*, which isn't
directly usable from your host shell. For ad-hoc querying against a Dockerized deployment, it's
generally simpler to `docker compose exec` into the container and run `duckdb` there, or to run
`bam snapshot` from inside the container:

```bash
docker compose exec app bam snapshot
```

## Step 5 — bring it down

```bash
docker compose down
```

The `bam_data` named volume persists across `down`/`up` cycles, so your ingested history survives.
To also drop the volume (start over from an empty database), pass `-v` — do this deliberately,
since it discards everything ingested so far.

## Next steps

- [Tutorial 4: troubleshooting](04-troubleshooting.md) — "no events arrive" as a full
  symptom-first checklist, including the Docker/macOS case above.
