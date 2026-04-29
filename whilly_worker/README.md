# whilly-worker

> Remote-worker meta-package for [Whilly v4.0](https://github.com/mshegolev/whilly-orchestrator).
> Installs the minimal worker dep closure (`httpx` + `pydantic` + `whilly.core` + `whilly.adapters.transport.client`) without the control-plane stack.

## What this package is — and isn't

`whilly-worker` is a **meta-package**: it ships no Python source of its own. Installing it pulls in `whilly-orchestrator[worker]`, which in turn provides the `whilly-worker` console script and the entire transitive code path the script needs to talk to a Whilly v4.0 control plane over HTTP.

The two install routes below are exactly equivalent — same dep tree, same console script, same versions. Pick whichever fits your tooling:

```bash
pip install whilly-worker                     # via this meta-package
pip install whilly-orchestrator[worker]       # via the extras
```

The reason both exist is that operators deploying a worker VM (or building a `python:3.12-slim` Docker image) tend to think *"I am installing the Whilly worker"*, not *"I am installing whilly-orchestrator with the [worker] extras"*. The shim makes the name match the role.

## Why the dep closure matters

Whilly v4.0 splits its runtime into two deployment shapes that share the pure domain layer (`whilly.core`) but never share a process:

| Shape          | Install                                           | Pulled deps                                                                      |
| -------------- | ------------------------------------------------- | -------------------------------------------------------------------------------- |
| Control plane  | `pip install whilly-orchestrator[server]`         | `asyncpg` + `fastapi` + `uvicorn` + `alembic` + `sqlalchemy[asyncio]` + base     |
| Remote worker  | `pip install whilly-worker`                       | `httpx` + base (`rich` + `pydantic` + `typer` + a few legacy v3 runtime deps)    |
| All-in-one dev | `pip install whilly-orchestrator[all]`            | both of the above                                                                |

The split exists for three concrete reasons (PRD §FR-1.5, §SC-6):

1.  **A worker VM never needs Postgres clients or a web server.** Pulling them in inflates the install footprint by ~50 MB and adds N+1 CVE-tracking surface for libraries the worker process literally never imports.
2.  **Failure mode is loud, not silent.** If a future refactor accidentally imports `asyncpg` from a worker-side module, an operator on a worker-only install gets an `ImportError` at process start instead of a silent compatibility shim. The [`.importlinter`](../docs/Whilly-v4-Architecture.md) contract (TASK-029) is the static guarantee; the dep split is the runtime one.
3.  **Image hygiene.** The Whilly worker Docker image (`scripts/run-v4-refactor.sh` driven, see TASK-024b demo) is a single `pip install whilly-worker` from `python:3.12-slim` — no need to know about extras, no need to keep a shopping list of pinned packages in the Dockerfile.

## Usage example

After install, the worker is driven by the `whilly-worker` console script (registered by `whilly-orchestrator`, made available because the meta-package depends on `[worker]`):

```bash
# Boot a worker that connects to a Whilly control plane and drains tasks
# until SIGTERM / SIGINT (Ctrl-C). The token comes from the bootstrap-token
# flow on the control plane (POST /workers/register, TASK-022a2).
whilly-worker \
    --connect https://control.whilly.example.com \
    --token "$WHILLY_WORKER_TOKEN" \
    --worker-id worker-vm-01

# Single-task mode — claim one task, run it, exit 0. Useful for cron-driven
# workers and for the integration tests in tests/integration/.
whilly-worker \
    --connect http://localhost:8000 \
    --token "$WHILLY_WORKER_TOKEN" \
    --once
```

Equivalent environment-variable form (lets you bake an image once and inject config at runtime):

```bash
export WHILLY_CONTROL_URL=https://control.whilly.example.com
export WHILLY_WORKER_TOKEN="$(cat /run/secrets/whilly-worker-token)"
export WHILLY_WORKER_ID=worker-vm-01

whilly-worker          # picks up env, runs forever
whilly-worker --once   # picks up env, drains one task
```

Missing `--token` / `WHILLY_WORKER_TOKEN` exits with code `2` and prints a hint, instead of trying to register anonymously and failing on the first `claim` round-trip — that fail-fast was added in TASK-022c.

## Compatibility

| `whilly-worker` version | Control-plane version  | Python  |
| ----------------------- | ---------------------- | ------- |
| `3.3.x` (this release)  | `whilly-orchestrator >= 3.3.0` (the v4.0 development line) | 3.12 / 3.13 |
| `4.0.0`                 | `whilly-orchestrator == 4.0.0` (TASK-033a release)         | 3.12 / 3.13 |

The dependency on `whilly-orchestrator[worker]` is **pinned to the exact same version** (`==3.3.0`), not loosely constrained. The wire protocol between `RemoteWorkerClient` and the FastAPI endpoints is part of the same release train, so worker and control plane must move together.

## Where the actual code lives

* `whilly.cli.worker` — `whilly-worker` console-script entry point (TASK-022c)
* `whilly.worker.remote` — async loop (`claim → run → complete | fail`) over HTTP (TASK-022b1/2/3)
* `whilly.adapters.transport.client` — the `RemoteWorkerClient` (TASK-022a1/2/3)
* `whilly.core` — pure domain layer, zero external deps

All of these ship inside the `whilly-orchestrator` wheel. This meta-package only gates which dependencies pip resolves around them.

## See also

* [`docs/Whilly-v4-Architecture.md`](../docs/Whilly-v4-Architecture.md) — Hexagonal layout: core / adapters / server / worker / cli
* [`docs/Whilly-v4-Worker-Protocol.md`](../docs/Whilly-v4-Worker-Protocol.md) — HTTP API spec the worker speaks
* [`docs/demo-remote-worker.sh`](../docs/demo-remote-worker.sh) — end-to-end SC-3 reproduction
