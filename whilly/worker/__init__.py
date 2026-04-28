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

Re-exports
----------
The public APIs of all sub-modules are re-exported at this level so callers
can ``from whilly.worker import run_local_worker`` /
``from whilly.worker import run_worker`` / ``from whilly.worker import
run_remote_worker`` without remembering sub-module paths. CLI entry points
(TASK-019c, TASK-022c) and tests use the package-level imports.
"""

from whilly.worker.local import (
    DEFAULT_IDLE_WAIT,
    RunnerCallable,
    WorkerStats,
    run_local_worker,
)
from whilly.worker.main import (
    DEFAULT_HEARTBEAT_INTERVAL,
    run_heartbeat_loop,
    run_worker,
)
from whilly.worker.remote import (
    RemoteRunnerCallable,
    RemoteWorkerStats,
    run_remote_heartbeat_loop,
    run_remote_worker,
    run_remote_worker_with_heartbeat,
)

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
