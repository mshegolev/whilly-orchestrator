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
* (future) :mod:`whilly.worker.remote` — TASK-022b, the same shape but over
  the HTTP transport.
* (future) :mod:`whilly.worker.main` — TASK-019b2, signal handling and
  graceful shutdown.

Re-exports
----------
The local-worker public API is re-exported at this level so callers can
``from whilly.worker import run_local_worker`` without remembering the
sub-module path.
"""

from whilly.worker.local import (
    DEFAULT_IDLE_WAIT,
    RunnerCallable,
    WorkerStats,
    run_local_worker,
)

__all__ = [
    "DEFAULT_IDLE_WAIT",
    "RunnerCallable",
    "WorkerStats",
    "run_local_worker",
]
