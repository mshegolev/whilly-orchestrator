# Whilly Orchestrator

Distributed orchestrator for AI coding agents — Postgres-backed task queue,
FastAPI control plane, remote workers over HTTP, append-only audit log.

[![PyPI](https://img.shields.io/pypi/v/whilly-orchestrator)](https://pypi.org/project/whilly-orchestrator/)
[![GitHub](https://img.shields.io/badge/github-mshegolev%2Fwhilly--orchestrator-blue)](https://github.com/mshegolev/whilly-orchestrator)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](https://github.com/mshegolev/whilly-orchestrator/blob/main/LICENSE)

> **Source repo:** https://github.com/mshegolev/whilly-orchestrator
> **PyPI:** https://pypi.org/project/whilly-orchestrator/
> **GHCR mirror:** `ghcr.io/mshegolev/whilly`

---

## Tags

| Tag         | What it is                                            |
|-------------|-------------------------------------------------------|
| `4.1.0`     | Immutable — pinned to git tag `v4.1.0`                |
| `4.1`       | Floating — latest patch in the 4.1 line               |
| `4`         | Floating — latest minor in the 4.x line               |
| `latest`    | Latest stable release from `main`                     |

Pre-release tags (`4.1.0-rc.1`, etc.) are pushed as immutable only — they
never claim `latest`, `4.1`, or `4`.

Multi-arch: every tag is a manifest list with **`linux/amd64`** and
**`linux/arm64`** — Apple Silicon and ARM servers Just Work™.

Image is signed with provenance + SBOM attestations
(`docker buildx imagetools inspect mshegolev/whilly:4.1.0` to verify).

---

## What's in the image

A single image with two roles, picked at runtime via the first CMD arg:

| Role            | Process                              | Listens on |
|-----------------|--------------------------------------|------------|
| `control-plane` | FastAPI (uvicorn) + Alembic migrate  | `:8000`    |
| `worker`        | `whilly-worker` (HTTP client)        | —          |
| `migrate`       | `alembic upgrade head` (one-shot)    | —          |
| `shell`         | `bash` for debugging                 | —          |

Built on `python:3.12-slim-bookworm`, runs as non-root user `whilly` (uid
1000), entrypoint via `tini` for proper signal handling. Healthcheck baked
in (`/health`) — Kubernetes probes work out of the box.

---

## Quick start (single host, all-in-one)

Все creds (postgres-пароль, worker bearer, bootstrap-token) держите в
`secrets.env` и инжектируйте через `--env-file`. Inline-DSN'ы и хардкод —
плохая практика, документация не должна их показывать.

```bash
# 1. Сгенерируйте секреты вне-репозиторно (например в gopass / Vault) —
#    тут не показываем как именно, чтобы не плодить хардкоды-плейсхолдеры.

# 2. Сложите всё в secrets.env (этот файл НЕ коммитится):
#    WHILLY_DATABASE_URL=...   (готовая asyncpg DSN)
#    WHILLY_WORKER_TOKEN=...   (output of: openssl rand -hex 32)
#    WHILLY_WORKER_BOOTSTRAP_TOKEN=...  (output of: openssl rand -hex 32)

# 3. Postgres (отдельный сервис, со своими creds — не из secrets.env):
docker run -d --name pg \
  --env-file ./pg.env \
  -p 127.0.0.1:5432:5432 postgres:15-alpine

# 4. Control plane (alembic upgrade head запускается на старте автоматически):
docker run -d --name whilly-control \
  --link pg:postgres \
  -p 8000:8000 \
  --env-file ./secrets.env \
  mshegolev/whilly:latest control-plane

# 5. Дождаться /health:
until curl -sf http://127.0.0.1:8000/health; do sleep 1; done

# 6. Worker (регистрируется через bootstrap, потом long-poll'ит задачи):
docker run -d --name whilly-worker \
  --link whilly-control:control \
  --env-file ./secrets.env \
  -e WHILLY_CONTROL_URL=http://control:8000 \
  -e WHILLY_PLAN_ID=demo \
  -v /usr/local/bin/claude:/usr/local/bin/claude:ro \
  -v $HOME/.config/anthropic:/home/whilly/.config/anthropic:ro \
  mshegolev/whilly:latest worker
```

For a turn-key compose stack (Postgres + control-plane + 2 workers) see
[`docker-compose.demo.yml`](https://github.com/mshegolev/whilly-orchestrator/blob/main/docker-compose.demo.yml)
in the source repo.

---

## docker-compose example

Все secrets (`POSTGRES_PASSWORD`, `WHILLY_DATABASE_URL`,
`WHILLY_WORKER_TOKEN`, `WHILLY_WORKER_BOOTSTRAP_TOKEN`) — из `.env` рядом
с compose или Docker secrets. **Никогда не хардкодьте их в compose-файле.**

```yaml
services:
  postgres:
    image: postgres:15-alpine
    env_file: ./.env       # POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 3s
      retries: 20

  control-plane:
    image: mshegolev/whilly:latest
    command: ["control-plane"]
    depends_on:
      postgres: { condition: service_healthy }
    env_file: ./.env       # WHILLY_DATABASE_URL + tokens
    ports: ["8000:8000"]

  worker:
    image: mshegolev/whilly:latest
    command: ["worker"]
    depends_on:
      control-plane: { condition: service_healthy }
    environment:
      WHILLY_CONTROL_URL: http://control-plane:8000
      WHILLY_WORKER_BOOTSTRAP_TOKEN: ${WHILLY_WORKER_BOOTSTRAP_TOKEN}
      WHILLY_PLAN_ID: my-plan
    deploy:
      replicas: 2     # масштабируется горизонтально, каждая реплика
                      # автоматически регистрируется через bootstrap-token
```

Run with `docker compose up -d`. Each worker replica registers itself via
the bootstrap token and gets a unique `worker_id` + per-worker bearer.
Postgres `FOR UPDATE SKIP LOCKED` ensures no two workers ever claim the
same task.

---

## Required environment variables

### Control plane (`control-plane` role)

| Var                              | Required | Description |
|----------------------------------|:---:|---|
| `WHILLY_DATABASE_URL`            | yes | asyncpg DSN to Postgres (set via secrets manager / Docker secret) |
| `WHILLY_WORKER_TOKEN`            | yes | Per-worker bearer token (legacy global; rotate per release) |
| `WHILLY_WORKER_BOOTSTRAP_TOKEN`  | yes | Cluster-join secret for `POST /workers/register` |

### Worker (`worker` role)

| Var                              | Required | Description |
|----------------------------------|:---:|---|
| `WHILLY_CONTROL_URL`             | yes | Control plane base URL (e.g. `http://control:8000`) |
| `WHILLY_PLAN_ID`                 | yes | Plan id this worker drains tasks from |
| `WHILLY_WORKER_BOOTSTRAP_TOKEN`  | yes¹ | Used to auto-register; required if `WHILLY_WORKER_TOKEN` is unset |
| `WHILLY_WORKER_TOKEN`            | yes¹ | Pre-issued per-worker bearer; skips auto-register when set |
| `CLAUDE_BIN`                     | no  | Path to Claude CLI binary inside the container (default: `claude` on PATH) |

¹ Either `WHILLY_WORKER_BOOTSTRAP_TOKEN` (auto-register on startup) or
`WHILLY_WORKER_TOKEN` (pre-issued) must be set. Bootstrap is recommended
for horizontally-scaled workers.

Full env-var reference: [`docs/Whilly-v4-Architecture.md`](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/Whilly-v4-Architecture.md).

---

## Verify the image

```bash
# Manifest (multi-arch list)
docker buildx imagetools inspect mshegolev/whilly:latest

# Image labels (OCI metadata)
docker inspect mshegolev/whilly:4.1.0 --format '{{json .Config.Labels}}' | jq

# Health
docker run --rm mshegolev/whilly:4.1.0 whilly --version
```

---

## Demo-image (with workshop fixtures)

For workshops / hands-on tutorials there's a separate
[`Dockerfile.demo`](https://github.com/mshegolev/whilly-orchestrator/blob/main/Dockerfile.demo)
in the source repo that bundles `tests/fixtures/fake_claude.sh` plus
example plans (`examples/demo/`). Build it locally:

```bash
git clone https://github.com/mshegolev/whilly-orchestrator
cd whilly-orchestrator
docker build -f Dockerfile.demo -t whilly-demo:latest .
```

Or just run [`workshop-demo.sh`](https://github.com/mshegolev/whilly-orchestrator/blob/main/workshop-demo.sh)
— end-to-end automated demo with 2 parallel workers in compose.

---

## Links

- Source: <https://github.com/mshegolev/whilly-orchestrator>
- PyPI: <https://pypi.org/project/whilly-orchestrator/>
- Architecture: [docs/Whilly-v4-Architecture.md](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/Whilly-v4-Architecture.md)
- Worker protocol: [docs/Whilly-v4-Worker-Protocol.md](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/Whilly-v4-Worker-Protocol.md)
- Issues: <https://github.com/mshegolev/whilly-orchestrator/issues>

License: MIT.
