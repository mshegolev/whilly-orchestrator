"""``whilly-worker`` console script — remote-worker entry point (TASK-022c, PRD FR-1.5, TC-6).

Composition root for the *remote* worker: this is the symmetric counterpart
to :mod:`whilly.cli.run` (which composes the *local* worker).

* :mod:`whilly.cli.run` opens an asyncpg pool, registers the worker via
  ``INSERT INTO workers``, instantiates a :class:`TaskRepository`, and
  drives :func:`whilly.worker.run_worker`.
* :mod:`whilly.cli.worker` (this module) opens an
  :class:`~whilly.adapters.transport.client.RemoteWorkerClient` over HTTP,
  assumes the worker row already exists on the control plane (registered
  out-of-band via the bootstrap-token flow — TASK-022a2), and drives
  :func:`whilly.worker.run_remote_worker_with_heartbeat`.

The two adapters never share a process: a *local* worker is colocated with
Postgres and would never need the HTTP transport, while a *remote* worker
runs on a different VM and intentionally has no asyncpg / FastAPI import
path (PRD SC-6 — see ``.importlinter`` contract). This split is the whole
point of the v4.0 refactor — see ``docs/Whilly-v4-Architecture.md``.

Why a separate console script ``whilly-worker``?
------------------------------------------------
The ``whilly`` console script (``whilly.cli:main``) bundles the legacy v3
parser, the ``plan`` subcommand, and ``whilly run``. All three pull in
asyncpg either eagerly or via the lazy-import seam. A standalone
``whilly-worker`` console script means a remote worker box only needs the
worker-flavour dependency closure (httpx + pydantic + ``whilly.core`` +
``whilly.adapters.transport.client``) — installing ``whilly-orchestrator``
on a worker VM but never running ``whilly`` works because Python imports
are pay-as-you-go. Wiring this through the ``whilly`` dispatcher would
nominally work too, but the AC for TASK-022c reads ``Entry point
зарегистрирован в pyproject.toml`` (singular) and operators expect the
binary name to match the worker's role rather than guess at a subcommand.

Required CLI flags / env vars
-----------------------------
============================  ==========================  =============================================
Flag                          Env var                     Meaning
============================  ==========================  =============================================
``--connect <url>``           ``WHILLY_CONTROL_URL``      Control-plane base URL (incl. scheme + port).
``--token <bearer>``          ``WHILLY_WORKER_TOKEN``     Per-worker bearer token (PRD FR-1.2, NFR-3).
``--plan <id>``               ``WHILLY_PLAN_ID``          Plan id this worker draws claims from.
============================  ==========================  =============================================

Optional flags
--------------
* ``--worker-id <id>`` — override the auto-generated identity (env:
  ``WHILLY_WORKER_ID``); defaults to ``<hostname>-<8-hex>`` so two workers
  on the same host don't collide. Same precedence chain as
  :mod:`whilly.cli.run`.
* ``--once`` — process exactly one task (whose terminal status is
  successfully written via ``client.complete`` or ``client.fail``) and
  exit 0. Wires through :func:`run_remote_worker_with_heartbeat`'s
  ``max_processed=1``. Idle polls and 409 lost-races do not count
  (intentional — see ``max_processed`` docstring on the loop).
* ``--heartbeat-interval <seconds>`` — override the 30s default
  (:data:`whilly.worker.remote.DEFAULT_HEARTBEAT_INTERVAL`). Mostly a
  test hook so an integration loop ticks observably.
* ``--max-iterations <n>`` — outer-loop cap. Test hook for CI runs that
  want a deterministic exit; production leaves it unset.

Why ``--token`` is the per-worker bearer, not the bootstrap secret
------------------------------------------------------------------
The bootstrap secret only authenticates ``POST /workers/register``. Once
the register call returns ``(worker_id, per_worker_token)``, every other
RPC must use the per-worker token (PRD FR-1.2 split, see
:mod:`whilly.adapters.transport.auth`). A ``whilly-worker`` instance
expects to *claim* tasks immediately — it has no reason to register first
unless an operator explicitly chose to pair these flows. Keeping
``--token`` bound to the per-worker bearer matches the steady-state RPC
surface (claim/complete/fail/heartbeat/release) and avoids the ambiguity
that would come with a single ``--token`` flag swapping meanings based on
the presence of ``--register``. A future ``whilly-worker register``
subcommand can land separately if operators want a bundled bootstrap.

Exit codes
----------
Mirrors :mod:`whilly.cli.run` so the v4 worker surface is consistent:

* ``0`` — worker loop returned normally (``--once`` completed one task,
  ``--max-iterations`` reached, or a SIGTERM/SIGINT-flipped ``stop``
  unwound the TaskGroup cleanly).
* ``2`` — *environment failure*: ``--connect`` / ``--token`` / ``--plan``
  missing, or argparse rejected the invocation. The AC reads "Отсутствие
  токена → exit 2 с подсказкой" — the diagnostic always names the env
  var so a fresh user can fix it without reading the source.

We do not map runtime exceptions onto exit codes here. A
:class:`~whilly.adapters.transport.client.AuthError` from the loop means
the operator gave us a wrong / rotated token — that's a configuration
error too, but it surfaces as an asyncio traceback because the
supervisor (Kubernetes, systemd) is what should react: log loudly,
restart, and let the env propagate the new token. Swallowing the
exception into ``return 2`` would conflate "you forgot the env" with
"your env is wrong" and make operator triage harder.

Why no ``--bootstrap-token`` flag here
--------------------------------------
The composition root is intentionally bare: register + token-rotation
flows are a separate concern owned by a future ``whilly-worker register``
subcommand. Mixing them here would tempt callers to share one token for
both purposes, which would defeat the FR-1.2 split (rotate the bootstrap
secret without invalidating per-worker bearers). When the register flow
lands, it will be a sibling subcommand (``whilly-worker register
--bootstrap-token X``) that prints the per-worker token + worker_id and
exits — the operator then re-invokes ``whilly-worker`` with those
values.

Synthetic Plan, no DB read
--------------------------
The remote worker only needs ``plan.id`` (passed to
:meth:`RemoteWorkerClient.claim`) and ``plan.name`` (rendered into the
agent prompt by :func:`whilly.core.prompts.build_task_prompt`). The
*tasks* are owned by the server and arrive via ``claim`` one at a time;
there is no benefit to fetching the full task list locally and the
worker has no SQL access by design. We therefore build a synthetic
:class:`whilly.core.models.Plan` with ``name = id`` (operators rarely
need the human-readable name in the worker journal, and a wire-level
``GET /plans/{id}`` doesn't exist today). If the prompt cosmetics
matter, ``--plan-name`` could be added later — punted from TASK-022c.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import uuid
from collections.abc import Sequence
from typing import Final

from whilly.adapters.runner import run_task
from whilly.adapters.transport.client import RemoteWorkerClient
from whilly.core.models import Plan, WorkerId
from whilly.worker import (
    DEFAULT_HEARTBEAT_INTERVAL,
    RemoteRunnerCallable,
    RemoteWorkerStats,
    run_remote_worker_with_heartbeat,
)

__all__ = [
    "CONTROL_URL_ENV",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "PLAN_ID_ENV",
    "WORKER_ID_ENV",
    "WORKER_TOKEN_ENV",
    "build_worker_parser",
    "main",
    "run_worker_command",
]

logger = logging.getLogger(__name__)

# Env vars — reuse the established ``WHILLY_WORKER_TOKEN`` /
# ``WHILLY_WORKER_ID`` names from :mod:`whilly.adapters.transport.auth` /
# :mod:`whilly.cli.run` so the same secret rotation / id pinning workflow
# the operator already knows applies to the remote worker. ``CONTROL_URL``
# and ``PLAN_ID`` are new (the local worker doesn't need them) — namespaced
# under ``WHILLY_`` like everything else.
CONTROL_URL_ENV: Final[str] = "WHILLY_CONTROL_URL"
WORKER_TOKEN_ENV: Final[str] = "WHILLY_WORKER_TOKEN"
PLAN_ID_ENV: Final[str] = "WHILLY_PLAN_ID"
WORKER_ID_ENV: Final[str] = "WHILLY_WORKER_ID"

# Exit codes — kept aligned with :mod:`whilly.cli.run` so callers comparing
# against the v4 CLI never see numbering drift between subcommands.
EXIT_OK: Final[int] = 0
EXIT_ENVIRONMENT_ERROR: Final[int] = 2


def build_worker_parser() -> argparse.ArgumentParser:
    """Build the ``whilly-worker ...`` argparse tree.

    Pulled into its own factory for symmetry with
    :func:`whilly.cli.run.build_run_parser` — tests can introspect the
    declared CLI surface without invoking the side-effecting handler
    (``run_worker_command`` opens an httpx client and would perform a
    DNS lookup on the first call).

    None of the flags are marked ``required=True`` at the argparse layer
    even though three of them effectively are — we want a richer
    diagnostic than argparse's "the following arguments are required:"
    message when the operator omits ``--token`` (the AC pins the
    "Отсутствие токена → exit 2 с подсказкой" path on a hint that names
    the env var). The hand-rolled validation in :func:`run_worker_command`
    handles that.
    """
    parser = argparse.ArgumentParser(
        prog="whilly-worker",
        description=(
            "Run a remote worker that connects to a Whilly control plane "
            "over HTTP and processes tasks for a given plan."
        ),
    )
    parser.add_argument(
        "--connect",
        dest="connect_url",
        default=None,
        help=(f"Control-plane base URL, e.g. http://control:8000 (env: {CONTROL_URL_ENV}). Required."),
    )
    parser.add_argument(
        "--token",
        dest="token",
        default=None,
        help=(
            f"Per-worker bearer token (env: {WORKER_TOKEN_ENV}). Required. "
            "This is the steady-state RPC token, not the cluster-wide "
            "bootstrap secret — see whilly/adapters/transport/auth.py for "
            "the FR-1.2 token split."
        ),
    )
    parser.add_argument(
        "--plan",
        dest="plan_id",
        default=None,
        help=(
            f"Plan id this worker draws claims from (env: {PLAN_ID_ENV}). "
            "Required. The server filters PENDING rows by plan_id; the worker "
            "never sees other plans' tasks."
        ),
    )
    parser.add_argument(
        "--worker-id",
        dest="worker_id",
        default=None,
        help=(
            f"Override the auto-generated worker id (env: {WORKER_ID_ENV}). "
            "Defaults to '<hostname>-<short-uuid>' so two workers on the same "
            "host don't collide on the workers PK."
        ),
    )
    parser.add_argument(
        "--once",
        dest="once",
        action="store_true",
        help=(
            "Process exactly one task to a terminal status (DONE or FAILED) "
            "and exit 0. Idle polls and 409 lost-race iterations do not count "
            "— a --once worker keeps trying until it owns a real outcome."
        ),
    )
    parser.add_argument(
        "--heartbeat-interval",
        dest="heartbeat_interval",
        type=float,
        default=None,
        help=(f"Seconds between worker heartbeat ticks (default: {DEFAULT_HEARTBEAT_INTERVAL}s)."),
    )
    parser.add_argument(
        "--max-iterations",
        dest="max_iterations",
        type=int,
        default=None,
        help=(
            "Cap the worker loop after N outer iterations (default: unbounded). "
            "Test hook for deterministic CI runs; production leaves it unset."
        ),
    )
    return parser


def run_worker_command(
    argv: Sequence[str],
    *,
    runner: RemoteRunnerCallable | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Entry point for the ``whilly-worker`` console script; returns the process exit code.

    ``runner`` is the unit-test injection seam — production callers
    (the console script's :func:`main`) leave it ``None`` so the
    production :func:`whilly.adapters.runner.run_task` is used. Tests
    pass an async closure so the CLI plumbing — argparse, env
    resolution, client construction, signal-handler wiring — is
    exercised end-to-end without spawning the Claude binary or a real
    HTTP server.

    ``install_signal_handlers`` mirrors
    :func:`whilly.cli.run.run_run_command`'s parameter of the same name.
    Production CLI invocations always run on the main thread of the main
    interpreter, so ``True`` is correct. Integration tests that drive
    this entry point via :func:`asyncio.to_thread` (because the test
    itself runs in pytest-asyncio's loop) pass ``False`` — the asyncio
    ``add_signal_handler`` call raises ``RuntimeError`` from a worker
    thread, and bypassing handler installation is the cleanest workaround
    that doesn't require restructuring the test harness.

    Stays synchronous on the outside so ops scripts can call it without
    an event loop; the async work is delegated to :func:`_async_worker`
    via :func:`asyncio.run`. This matches the legacy ``whilly`` and
    ``whilly run`` shapes — one CLI surface, one asyncio entry per
    invocation.
    """
    parser = build_worker_parser()
    args = parser.parse_args(list(argv))

    # CLI flag > env > error. The hand-rolled validation lets us produce
    # one diagnostic per missing input that names the env var, instead of
    # argparse's "the following arguments are required" message that hides
    # the env override entirely.
    connect_url = args.connect_url or os.environ.get(CONTROL_URL_ENV)
    if not connect_url:
        print(
            f"whilly-worker: --connect is required (or set {CONTROL_URL_ENV}). "
            "Point it at the control-plane base URL, e.g. http://control:8000.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    token = args.token or os.environ.get(WORKER_TOKEN_ENV)
    if not token:
        print(
            f"whilly-worker: --token is required (or set {WORKER_TOKEN_ENV}). "
            "This is the per-worker bearer issued at registration; "
            "the bootstrap secret is for `whilly-worker register` only.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    plan_id = args.plan_id or os.environ.get(PLAN_ID_ENV)
    if not plan_id:
        print(
            f"whilly-worker: --plan is required (or set {PLAN_ID_ENV}). "
            "This is the plan id imported via `whilly plan import`.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    worker_id = _resolve_worker_id(args.worker_id)
    effective_runner: RemoteRunnerCallable = runner if runner is not None else run_task
    heartbeat_interval = args.heartbeat_interval if args.heartbeat_interval is not None else DEFAULT_HEARTBEAT_INTERVAL
    # ``--once`` translates to ``max_processed=1`` on the remote loop.
    # Mutually-exclusive with the existing ``max_iterations`` cap: both
    # can be set, the first to fire wins (the loop honours either).
    max_processed = 1 if args.once else None

    stats = asyncio.run(
        _async_worker(
            connect_url=connect_url,
            token=token,
            plan_id=plan_id,
            worker_id=worker_id,
            runner=effective_runner,
            heartbeat_interval=heartbeat_interval,
            max_iterations=args.max_iterations,
            max_processed=max_processed,
            install_signal_handlers=install_signal_handlers,
        )
    )

    print(
        (
            f"whilly-worker: worker {worker_id!r} finished — "
            f"iterations={stats.iterations} completed={stats.completed} "
            f"failed={stats.failed} idle_polls={stats.idle_polls} "
            f"released_on_shutdown={stats.released_on_shutdown}"
        ),
        file=sys.stderr,
    )
    return EXIT_OK


def _resolve_worker_id(cli_override: str | None) -> WorkerId:
    """Pick the worker id; CLI flag > env > auto-generated.

    Auto-generated form is ``<hostname>-<8-char-uuid-prefix>``. Same
    rationale as :func:`whilly.cli.run._resolve_worker_id` — keeping the
    two CLIs in lock-step on identity generation means an operator can
    swap a local worker for a remote one against the same plan without
    relearning identity conventions. Eight hex chars give 4B distinct ids,
    plenty for the lifetime of a single deployment, and the shorter id
    reads cleanly in logs.

    The function is duplicated rather than shared because
    :mod:`whilly.cli.run` lives behind the asyncpg-importing dispatcher
    and importing it from this module would defeat the dependency-light
    point of the standalone ``whilly-worker`` script.
    """
    if cli_override:
        return cli_override
    env_override = os.environ.get(WORKER_ID_ENV)
    if env_override:
        return env_override
    suffix = uuid.uuid4().hex[:8]
    return f"{socket.gethostname()}-{suffix}"


async def _async_worker(
    *,
    connect_url: str,
    token: str,
    plan_id: str,
    worker_id: WorkerId,
    runner: RemoteRunnerCallable,
    heartbeat_interval: float,
    max_iterations: int | None,
    max_processed: int | None,
    install_signal_handlers: bool,
) -> RemoteWorkerStats:
    """Open the HTTP client, build a synthetic Plan, run the loop.

    The ``async with`` over :class:`RemoteWorkerClient` is the only
    side-effect surface — the loop owns connection lifecycle, signal
    handler installation, and TaskGroup unwinding. We don't catch
    anything: an :class:`AuthError` (bad token), a :class:`ServerError`
    (control plane down), or any other transport failure surfaces as a
    traceback so the supervisor (Kubernetes, systemd, tmux) knows the
    pod failed and restart policies kick in. Mapping these onto exit
    codes here would conflate "the env was wrong" (already returned 2
    from the sync wrapper) with "the cluster is broken", and operator
    triage would lose the typed exception information.

    The synthetic ``Plan(id=plan_id, name=plan_id)`` is documented in the
    module docstring — the worker doesn't need the full task list and the
    server has no ``GET /plans/{id}`` today.
    """
    plan = Plan(id=plan_id, name=plan_id)
    async with RemoteWorkerClient(connect_url, token) as client:
        logger.info(
            "whilly-worker: connecting to %s as worker_id=%s plan_id=%s once=%s",
            connect_url,
            worker_id,
            plan_id,
            max_processed == 1,
        )
        return await run_remote_worker_with_heartbeat(
            client,
            runner,
            plan,
            worker_id,
            heartbeat_interval=heartbeat_interval,
            max_iterations=max_iterations,
            max_processed=max_processed,
            install_signal_handlers=install_signal_handlers,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point — registered as ``whilly-worker`` in pyproject.toml.

    Thin wrapper over :func:`run_worker_command` that resolves
    ``argv`` from :data:`sys.argv` when called as the binary. Tests
    invoke :func:`run_worker_command` directly with a list to avoid
    poking at ``sys.argv``.
    """
    return run_worker_command(sys.argv[1:] if argv is None else list(argv))
