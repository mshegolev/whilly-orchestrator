"""Worker layer for Whilly v4.0 (PRD FR-1.6, TC-8).

The worker package is the *composer* in the Hexagonal architecture: it pulls
together the pure :mod:`whilly.core` domain (state machine, scheduler,
prompts) and the I/O-side :mod:`whilly.adapters` (Postgres repository,
Claude CLI subprocess) into a single async loop that actually runs tasks.

Sub-modules
-----------
* :mod:`whilly.worker.local` — TASK-019a, the bare-bones local async loop
  ``claim_task → start_task → run_task → complete_task | fail_task``. No
  heartbeat, no signals, no CLI.
* :mod:`whilly.worker.main` — TASK-019b1, the local-worker composition root
  that pairs :func:`local.run_local_worker` with a parallel heartbeat task
  under one :class:`asyncio.TaskGroup`. SIGTERM/SIGINT plumbing extends this
  in TASK-019b2.
* :mod:`whilly.worker.remote` — TASK-022b1 (bare loop) + TASK-022b2 (heartbeat
  composition). Same outer shape as the local worker but over the HTTP
  transport. Signal handling (TASK-022b3) lands in this module too,
  mirroring the 019b1 / 019b2 slicing on the local side — the only
  asymmetry is that the remote side colocates loop + supervisor in one
  file rather than the local side's ``local.py`` + ``main.py`` split.

Worker-import-purity discipline (PRD SC-6, fix-m1-whilly-worker-fastapi-leak)
-----------------------------------------------------------------------------
The remote worker entry (:mod:`whilly.cli.worker`) only needs
:mod:`whilly.worker.remote` (httpx + pydantic + ``whilly.core``). The
local-worker submodules — :mod:`whilly.worker.local` and
:mod:`whilly.worker.main` — transitively import
:mod:`whilly.adapters.db.repository`, which loads ``asyncpg``. Eagerly
re-exporting them at the package level breaks ``pip install
whilly-orchestrator[worker] && whilly-worker --help`` because asyncpg
is not in the ``[worker]`` extras dep closure. We therefore defer the
``local`` / ``main`` re-exports through PEP 562 module-level
``__getattr__``: control-plane code that imports
``whilly.worker.run_local_worker`` still works (the lookup imports
``whilly.worker.local`` on demand), but a bare
``import whilly.worker.remote`` from the worker entry no longer drags
the asyncpg-backed local loop into ``sys.modules``.

Re-exports
----------
The public APIs of all sub-modules are re-exported at this level so callers
can ``from whilly.worker import run_local_worker`` /
``from whilly.worker import run_worker`` / ``from whilly.worker import
run_remote_worker`` without remembering sub-module paths. CLI entry points
(TASK-019c, TASK-022c) and tests use the package-level imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from whilly.worker.remote import (
    DEFAULT_HEARTBEAT_INTERVAL,
    RemoteRunnerCallable,
    RemoteWorkerStats,
    run_remote_heartbeat_loop,
    run_remote_worker,
    run_remote_worker_with_heartbeat,
)

if TYPE_CHECKING:
    from whilly.worker.local import (
        DEFAULT_IDLE_WAIT,
        RunnerCallable,
        WorkerStats,
        run_local_worker,
    )
    from whilly.worker.main import (
        run_heartbeat_loop,
        run_worker,
    )


# Names sourced from :mod:`whilly.worker.local`. Resolving any of them
# through :func:`__getattr__` imports that submodule (which pulls
# asyncpg via the repository layer); subsequent lookups bind directly.
_LOCAL_NAMES: frozenset[str] = frozenset(
    {
        "DEFAULT_IDLE_WAIT",
        "RunnerCallable",
        "WorkerStats",
        "run_local_worker",
    }
)

# Names sourced from :mod:`whilly.worker.main` (the local-worker
# composition root with heartbeat + signal handlers). Imports both
# ``local`` and ``main``, so the same asyncpg surface is touched.
_MAIN_NAMES: frozenset[str] = frozenset(
    {
        "run_heartbeat_loop",
        "run_worker",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily import ``local`` / ``main`` submodules on first attribute access.

    PEP 562 module-level ``__getattr__``. The package-level
    ``DEFAULT_HEARTBEAT_INTERVAL`` re-export historically came from
    :mod:`whilly.worker.main`; it now comes from :mod:`whilly.worker.remote`
    (where the constant is also defined, with the same value) so the
    remote worker entry resolves it without dragging the local-worker
    closure.
    """
    if name in _LOCAL_NAMES:
        from whilly.worker import local as _local

        value = getattr(_local, name)
    elif name in _MAIN_NAMES:
        from whilly.worker import main as _main

        value = getattr(_main, name)
    else:
        raise AttributeError(f"module 'whilly.worker' has no attribute {name!r}")
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_IDLE_WAIT",
    "RemoteRunnerCallable",
    "RemoteWorkerStats",
    "RunnerCallable",
    "WorkerStats",
    "run_heartbeat_loop",
    "run_local_worker",
    "run_remote_heartbeat_loop",
    "run_remote_worker",
    "run_remote_worker_with_heartbeat",
    "run_worker",
]
