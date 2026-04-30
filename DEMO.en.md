# Whilly Demo — local & 2-container Docker (English brief)

> Short companion to [`DEMO.md`](DEMO.md) (Russian, full). Same artefacts,
> condensed walkthrough — written for an English-speaking audience or a
> shorter slot.

In v4 Whilly is three components:

```
Postgres 15  ◄──  control-plane (FastAPI / uvicorn)  ◄──  whilly-worker (httpx)
                  application container #1                application container #2
```

The demo shows the same flow in two shapes: locally on your laptop, and as
two application containers (control-plane + worker) plus a Postgres
container.

---

## Files added by this demo

| Path                                | Purpose |
|-------------------------------------|---------|
| `Dockerfile.demo`                   | Single image, two roles (control-plane / worker) |
| `docker/entrypoint.sh`              | Role dispatcher inside the image |
| `docker-compose.demo.yml`           | Postgres + control-plane + worker (+ optional seed) |
| `examples/demo/tasks.json`          | 4-task demo plan |
| `examples/demo/PRD-demo.md`         | Plan PRD context |

The image uses `tests/fixtures/fake_claude.sh` (already in repo) so the
demo runs deterministically without burning real Claude tokens.

---

## Local mode (host machine)

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e '.[all]'

# 2. Postgres
./scripts/db-up.sh
# Demo Postgres credentials default to whilly/whilly (see docker-compose.yml).
export WHILLY_DATABASE_URL="postgresql://${POSTGRES_USER:-whilly}:${POSTGRES_PASSWORD:-whilly}@localhost:5432/whilly"
alembic upgrade head

# 3. Import the demo plan
whilly plan import examples/demo/tasks.json
whilly plan show demo

# 4. Run all-in-one (control plane embedded in the worker process)
CLAUDE_BIN="$PWD/tests/fixtures/fake_claude.sh" \
  whilly run --plan demo --max-iterations 10

# 5. Inspect
whilly plan show demo
psql "$WHILLY_DATABASE_URL" -c \
  "SELECT task_id, event_type FROM events WHERE plan_id='demo' ORDER BY id;"
```

For a host-side distributed rehearsal (uvicorn + `whilly-worker` in two
terminals) see [`docs/demo-remote-worker.sh`](docs/demo-remote-worker.sh).

---

## Docker mode (2 application containers + DB)

The compose file ships three services. **Logically** that's "DB + 2 whilly
application containers":

| Service        | Image                | Role |
|----------------|----------------------|------|
| `postgres`     | `postgres:15-alpine` | State machine + audit log |
| `control-plane`| `whilly-demo:latest` | FastAPI / uvicorn (port 8000) |
| `worker`       | `whilly-demo:latest` | `whilly-worker` HTTP client |

```bash
# 1. Build + start
docker compose -f docker-compose.demo.yml up -d --build

# 2. Import plan from inside control-plane
docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan import examples/demo/tasks.json

docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan show demo

# 3. Watch the worker drain the queue
docker compose -f docker-compose.demo.yml logs -f worker

# 4. Audit log
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c \
  "SELECT task_id, event_type FROM events WHERE plan_id='demo' ORDER BY id;"

# 5. Tear down (drop the volume too)
docker compose -f docker-compose.demo.yml down -v
```

### Bonus: scale workers

```bash
docker compose -f docker-compose.demo.yml up -d --scale worker=2
```

Two workers, one Postgres queue, `FOR UPDATE SKIP LOCKED` does the work —
nice slide moment.

---

## Presentation script (~3 minutes)

1. **Slide** — the architecture diagram above.
2. **Terminal A** — `docker compose -f docker-compose.demo.yml up -d`.
3. **Terminal A** — `whilly plan show demo` (all tasks `PENDING`).
4. **Terminal B** — `docker compose logs -f worker` (live).
5. **Terminal A** — `whilly plan import examples/demo/tasks.json`.
6. **Terminal B** shows CLAIM → COMPLETE flowing in real time.
7. **Terminal A** — `whilly plan show demo` (all `DONE`).
8. **Terminal A** — `SELECT … FROM events`. Audit log is intact.

---

## Troubleshooting

- **`worker` cannot reach control-plane** — inside the docker network the
  hostname is `control-plane`, not `localhost`. The compose file sets
  `WHILLY_CONTROL_URL=http://control-plane:8000` for that reason.
- **Use real Claude instead of the stub** — drop `CLAUDE_BIN` from the
  `worker` service env and bind-mount the binary + your Anthropic config
  into the container.
- **Reset state completely** — `docker compose -f docker-compose.demo.yml down -v`
  drops the `whilly_demo_pgdata` volume.

Further reading: [`README.md`](README.md), [`docs/Whilly-v4-Architecture.md`](docs/Whilly-v4-Architecture.md),
[`docs/Whilly-v4-Worker-Protocol.md`](docs/Whilly-v4-Worker-Protocol.md).
