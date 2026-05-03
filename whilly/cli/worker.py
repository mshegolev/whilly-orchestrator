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
import ipaddress
import logging
import os
import socket
import sys
import uuid
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from whilly.adapters.runner.claude_cli import run_task
from whilly.adapters.transport.client import RemoteWorkerClient
from whilly.core.models import Plan, WorkerId
from whilly.worker.remote import (
    DEFAULT_HEARTBEAT_INTERVAL,
    RemoteRunnerCallable,
    RemoteWorkerStats,
    run_remote_worker_with_heartbeat,
)

__all__ = [
    "BOOTSTRAP_TOKEN_ENV",
    "CONTROL_URL_ENV",
    "EXIT_CONNECT_ERROR",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "PLAN_ID_ENV",
    "WORKER_ID_ENV",
    "WORKER_TOKEN_ENV",
    "InsecureSchemeError",
    "UrlValidationError",
    "build_connect_parser",
    "build_register_parser",
    "build_worker_parser",
    "check_opencode_groq_credentials",
    "classify_control_url",
    "main",
    "run_connect_command",
    "run_register_command",
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

#: Env var holding the cluster-join secret. Aliased here from
#: :data:`whilly.adapters.transport.auth.BOOTSTRAP_TOKEN_ENV` so the
#: ``whilly worker register`` subcommand reads the same value the
#: control plane writes (single source of truth for the env name).
BOOTSTRAP_TOKEN_ENV: Final[str] = "WHILLY_WORKER_BOOTSTRAP_TOKEN"

# Exit codes — kept aligned with :mod:`whilly.cli.run` so callers comparing
# against the v4 CLI never see numbering drift between subcommands.
#
# * ``EXIT_OK`` (0)                    — success.
# * ``EXIT_CONNECT_ERROR`` (1)         — ``whilly worker connect`` failed
#   *after* argparse / env validation: bad bootstrap token, control-plane
#   unreachable, scheme guard rejected the URL, or any other runtime
#   precondition failed. Per the M1 feature description, ``connect``
#   uses exit ``1`` for these cases (distinct from missing-required
#   diagnostics, which keep the legacy ``2`` to match :mod:`whilly.cli.run`).
# * ``EXIT_ENVIRONMENT_ERROR`` (2)     — required CLI flag / env var
#   missing on the worker loop. Pre-existing convention.
EXIT_OK: Final[int] = 0
EXIT_CONNECT_ERROR: Final[int] = 1
EXIT_ENVIRONMENT_ERROR: Final[int] = 2


# ---------------------------------------------------------------------------
# URL classification + scheme guard (M1, ``--insecure``)
# ---------------------------------------------------------------------------


class UrlValidationError(ValueError):
    """Raised when a control-plane URL is malformed in a way the operator can fix.

    Carries a human-readable reason that callers print verbatim to
    stderr. ``code`` is reserved for future structured logging.
    """


class InsecureSchemeError(UrlValidationError):
    """Raised when plain HTTP is targeted at a non-loopback host without ``--insecure``."""


# Hostnames the loopback exemption recognises by name (case-insensitive,
# exact match). Substring matches are *intentionally* not supported —
# ``localhost.evil.example`` would be a public DNS name and must NOT be
# treated as loopback (VAL-M1-INSECURE-903). Empty hostnames (URL with
# no host, e.g. ``http:///path``) never qualify.
_LOOPBACK_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost"})


def _is_loopback_host(host: str) -> bool:
    """Return True iff ``host`` is a well-known loopback target.

    Recognised:

    * IPv4 addresses inside ``127.0.0.0/8``.
    * IPv6 ``::1`` (the IPv6 loopback).
    * The literal hostname ``localhost`` (case-insensitive, exact match).

    Explicitly NOT recognised:

    * ``0.0.0.0`` / ``[::]`` — wildcard binds, never a routable target.
    * RFC1918 / link-local ranges (``192.168.0.0/16``, ``10.0.0.0/8``,
      ``172.16.0.0/12``, ``169.254.0.0/16``) — private but not loopback.
    * Any DNS name that merely *contains* "localhost".
    """
    if not host:
        return False
    bare = host.strip("[]").lower()
    if bare in _LOOPBACK_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(bare)
    except ValueError:
        # Not an IP literal and not a recognised loopback name → not loopback.
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return addr in ipaddress.ip_network("127.0.0.0/8")
    # IPv6: only ::1 counts (is_loopback also covers IPv4-mapped, which
    # is fine — those would have been caught by the v4 branch above).
    return addr.is_loopback


def classify_control_url(url: str) -> tuple[str, str, int]:
    """Validate ``url`` and return ``(scheme, host, port)``.

    Raises :class:`UrlValidationError` on:

    * Empty / whitespace-only URL.
    * Missing ``http://`` / ``https://`` scheme.
    * Empty hostname.
    * Out-of-range / non-numeric port.

    A URL with a non-trivial path (anything after the host[:port] beyond
    a single trailing slash) is rejected — the operator must point the
    flag at the bare control-plane base URL. We picked option (b) of
    VAL-M1-CONNECT-902 (reject) over (a) (use as base) because every
    register / claim path on the control-plane is rooted at ``/`` today,
    so a path-bearing URL is overwhelmingly an operator typo (e.g.
    pasting the dashboard URL).

    Caller is responsible for the ``--insecure`` decision; this function
    only classifies the URL.
    """
    if not url or not url.strip():
        raise UrlValidationError("control-plane URL is empty")
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise UrlValidationError(f"control-plane URL {url!r} is missing a scheme — prefix with http:// or https://")
    host = parts.hostname or ""
    if not host:
        raise UrlValidationError(f"control-plane URL {url!r} has no host component")
    try:
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        # urllib raises on out-of-range ports (>65535) at attribute access.
        raise UrlValidationError(f"control-plane URL {url!r} has invalid port: {exc}") from exc
    if not 1 <= port <= 65535:
        raise UrlValidationError(f"control-plane URL {url!r} has out-of-range port {port}")
    # Reject path / query / fragment. A bare trailing slash is fine — we
    # canonicalise it away. ``parts.path`` is "" or "/" in that case.
    path = parts.path or ""
    if path not in ("", "/") or parts.query or parts.fragment:
        raise UrlValidationError(
            f"control-plane URL {url!r} must not include a path, query, or fragment "
            "(point it at the base URL, e.g. http://host:8000)"
        )
    return parts.scheme, host, port


def enforce_scheme_guard(url: str, *, insecure: bool) -> tuple[str, str, int]:
    """Validate URL + apply the ``--insecure`` rule; return ``(scheme, host, port)``.

    Plain HTTP to a non-loopback host requires ``--insecure``; otherwise
    raises :class:`InsecureSchemeError`. HTTPS always passes (the TLS
    layer enforces cert validation regardless of ``--insecure``).
    """
    scheme, host, port = classify_control_url(url)
    if scheme == "https":
        return scheme, host, port
    if _is_loopback_host(host):
        return scheme, host, port
    if not insecure:
        raise InsecureSchemeError(
            f"plain HTTP to non-loopback host {host!r} requires --insecure "
            "(use HTTPS or pass --insecure if you accept the risk)"
        )
    return scheme, host, port


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
    parser.add_argument(
        "--insecure",
        dest="insecure",
        action="store_true",
        help=(
            "Allow plain HTTP to a non-loopback control-plane URL. Required "
            "when --connect points at http://<remote-host>:... over plaintext; "
            "loopback (127.0.0.0/8, ::1, localhost) is exempt. Always prints a "
            "warning to stderr when set."
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

    plan_id = args.plan_id or os.environ.get(PLAN_ID_ENV)
    if not plan_id:
        print(
            f"whilly-worker: --plan is required (or set {PLAN_ID_ENV}). "
            "This is the plan id imported via `whilly plan import`.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    token = args.token or os.environ.get(WORKER_TOKEN_ENV)
    if not token:
        # Keyring-resume read path (M2, VAL-M1-DEMO-009): when neither
        # ``--token`` nor ``WHILLY_WORKER_TOKEN`` is provided, fall back
        # to a bearer previously stored by ``whilly worker connect``
        # (or any other caller of ``store_worker_credential``). Lookup
        # is keyed by the canonical control URL; ``plan_id`` is passed
        # through for forward-compatibility with future per-plan scoping.
        # Any storage-backend error is treated as "no token" so the
        # canonical diagnostic below covers all "operator forgot to
        # provide a bearer" cases, not just the env/flag branches.
        try:
            from whilly.secrets import fetch_worker_credential

            token = fetch_worker_credential(connect_url, plan_id)
        except Exception as exc:
            logger.warning(
                "whilly-worker: keychain lookup failed for %s: %s",
                connect_url,
                exc,
            )
            token = None
    if not token:
        print(
            "whilly-worker: no token: pass --token, set "
            f"{WORKER_TOKEN_ENV}, or run `whilly worker connect` to "
            "store one in the keychain.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    # Apply the M1 scheme guard: plain HTTP to a non-loopback host is
    # an operator footgun — refuse unless ``--insecure`` is explicit.
    # HTTPS always passes; loopback always passes. When the operator
    # opts in to ``--insecure``, surface a stderr warning so they have a
    # chance to notice in CI logs.
    try:
        scheme, host, _port = enforce_scheme_guard(connect_url, insecure=args.insecure)
    except UrlValidationError as exc:
        print(f"whilly-worker: {exc}", file=sys.stderr)
        return EXIT_CONNECT_ERROR
    if args.insecure and scheme == "http" and not _is_loopback_host(host):
        print(
            f"whilly-worker: warning — using plain HTTP to non-loopback host {host!r} "
            "(--insecure). Prefer HTTPS in production.",
            file=sys.stderr,
        )

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


def build_register_parser() -> argparse.ArgumentParser:
    """Build the ``whilly worker register ...`` argparse tree (TASK-101, VAL-AUTH-040).

    The register flow is intentionally separate from the
    :func:`build_worker_parser` loop parser:

    * **Different auth.** ``register`` carries the cluster-join secret
      (``WHILLY_WORKER_BOOTSTRAP_TOKEN``), the worker loop carries the
      per-worker bearer token. Sharing one ``--token`` flag would
      conflate them.
    * **Different lifecycle.** ``register`` is a one-shot RPC that
      prints the plaintext token and exits 0; the worker loop runs
      indefinitely. A single argparse parser would drag the worker-
      loop knobs (``--once``, ``--heartbeat-interval``, ...) into
      ``register --help`` for no reason.

    Output contract
    ---------------
    On success the command writes two lines to stdout, one ``key:
    value`` per line, terminated by newlines:

    .. code-block:: text

       worker_id: w-<urlsafe-suffix>
       token: <opaque-bearer>

    The format is grep-able from shell scripts (``whilly worker
    register | grep '^token:' | cut -d' ' -f2``) and matches the
    VAL-AUTH-040 evidence assertion ("TUI snapshot shows non-empty
    ``token: ...`` line"). Operators in interactive TTYs can copy-
    paste either field directly.

    Why no JSON output?
        JSON would force operators to install ``jq`` to peel off one
        field for the next command (``whilly worker --token=...``).
        The ``key: value`` shape is shell-native and the CLI is meant
        for human + ad-hoc-script consumption — a future ``--json``
        flag can land if a tool requires structured output.
    """
    parser = argparse.ArgumentParser(
        prog="whilly worker register",
        description=(
            "Register a new worker with the control plane and print its "
            "plaintext per-worker bearer token. The plaintext is shown "
            "exactly once — capture it for subsequent `whilly worker` "
            "invocations or persist it via your secret manager."
        ),
    )
    parser.add_argument(
        "--connect",
        dest="connect_url",
        default=None,
        help=(f"Control-plane base URL, e.g. http://control:8000 (env: {CONTROL_URL_ENV}). Required."),
    )
    parser.add_argument(
        "--bootstrap-token",
        dest="bootstrap_token",
        default=None,
        help=(
            f"Cluster-join secret (env: {BOOTSTRAP_TOKEN_ENV}). Required. "
            "Authenticates POST /workers/register; rotate independently "
            "from per-worker bearers — see whilly/adapters/transport/auth.py."
        ),
    )
    parser.add_argument(
        "--hostname",
        dest="hostname",
        default=None,
        help=(
            "Hostname the new worker self-reports. Defaults to socket.gethostname() "
            "if omitted, matching `whilly worker`'s identity convention."
        ),
    )
    return parser


def run_register_command(argv: Sequence[str]) -> int:
    """Execute ``whilly worker register ...`` — return exit code (TASK-101, VAL-AUTH-040).

    Parses the register subcommand args, opens a one-shot
    :class:`RemoteWorkerClient` bound to the bootstrap secret, calls
    :meth:`RemoteWorkerClient.register`, prints the plaintext token to
    stdout in the documented ``key: value`` shape, and exits 0.

    The transport client requires a non-empty ``token`` argument even
    on the register-only path (it's the per-worker bearer field, not
    used by ``register`` itself). We pass a placeholder string so the
    constructor invariant is satisfied without exposing the real
    bootstrap secret as the per-worker token by accident.

    Exit codes
    ----------
    * ``0`` — registration succeeded; plaintext + worker_id printed.
    * ``2`` — environment failure (missing ``--connect`` /
      ``--bootstrap-token``); same as the worker loop.
    * Any other failure (network error, server 4xx/5xx) propagates as
      an asyncio traceback; the supervisor decides whether to retry —
      mirrors :func:`run_worker_command`'s "let typed exceptions
      surface" policy.
    """
    parser = build_register_parser()
    args = parser.parse_args(list(argv))

    connect_url = args.connect_url or os.environ.get(CONTROL_URL_ENV)
    if not connect_url:
        print(
            f"whilly worker register: --connect is required (or set {CONTROL_URL_ENV}). "
            "Point it at the control-plane base URL, e.g. http://control:8000.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    bootstrap_token = args.bootstrap_token or os.environ.get(BOOTSTRAP_TOKEN_ENV)
    if not bootstrap_token:
        print(
            f"whilly worker register: --bootstrap-token is required (or set {BOOTSTRAP_TOKEN_ENV}). "
            "This is the cluster-join secret — distinct from the per-worker bearer.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    hostname = args.hostname or socket.gethostname()

    response = asyncio.run(_async_register(connect_url, bootstrap_token, hostname))
    # ``key: value`` lines on stdout, one field per line. Stdout is the
    # right channel because the contract is "scriptable extraction"
    # (``... | grep '^token:'``); progress / diagnostics go to stderr.
    sys.stdout.write(f"worker_id: {response.worker_id}\n")
    sys.stdout.write(f"token: {response.token}\n")
    sys.stdout.flush()
    return EXIT_OK


async def _async_register(
    connect_url: str,
    bootstrap_token: str,
    hostname: str,
) -> "RegisterResponse":  # noqa: F821 — resolved at runtime, see import below
    """Open a short-lived :class:`RemoteWorkerClient` and call ``register``.

    The local import keeps the cold-start cost of the ``register``
    subcommand off the hot path — a worker that only ever runs the
    main loop never imports the schemas module. Same pattern the rest
    of :mod:`whilly.cli` uses for sub-CLI dispatch.
    """
    from whilly.adapters.transport.client import RemoteWorkerClient
    from whilly.adapters.transport.schemas import RegisterResponse  # noqa: F401 — re-exported via TYPE_CHECKING shape

    # The transport client requires ``token`` to be non-empty even on
    # the register path; the placeholder is never sent over the wire
    # because :meth:`RemoteWorkerClient.register` switches to the
    # bootstrap branch.
    async with RemoteWorkerClient(
        connect_url,
        token="register-placeholder",
        bootstrap_token=bootstrap_token,
    ) as client:
        return await client.register(hostname)


# ---------------------------------------------------------------------------
# ``whilly worker connect <url>`` — one-shot bootstrap (M1)
# ---------------------------------------------------------------------------


def build_connect_parser() -> argparse.ArgumentParser:
    """Build the ``whilly worker connect <url> ...`` argparse tree (M1).

    The connect flow is the operator's one-line bootstrap:

    1. Validate the URL (scheme guard, port range, no path).
    2. Validate the bootstrap token (non-empty after ``.strip()``).
    3. ``POST /workers/register`` with the bootstrap token.
    4. Persist the per-worker bearer in the OS keychain (with file fallback).
    5. ``os.execvp`` into ``whilly-worker --connect <url> --token <bearer>
       --plan <p>`` so the operator's process *is* the worker.

    Output contract on stdout (line-oriented, ``key: value``, no banners
    between):

    .. code-block:: text

       worker_id: w-XXXXXXXX
       token: <plaintext>

    Operators can ``... | grep '^token:' | cut -d' ' -f2`` to extract
    the bearer.
    """
    parser = argparse.ArgumentParser(
        prog="whilly worker connect",
        description=(
            "One-line worker bootstrap: register against the control plane, "
            "store the per-worker bearer in the OS keychain (or chmod-600 "
            "fallback file), then exec into whilly-worker."
        ),
    )
    parser.add_argument(
        "control_url",
        help=(
            "Control-plane base URL, e.g. http://127.0.0.1:8000 or "
            "https://control.example.com. Plain HTTP to a non-loopback host "
            "requires --insecure."
        ),
    )
    parser.add_argument(
        "--bootstrap-token",
        dest="bootstrap_token",
        default=None,
        help=(
            f"Cluster-join secret (env: {BOOTSTRAP_TOKEN_ENV}). Required. "
            "Authenticates POST /workers/register; rotate independently from "
            "per-worker bearers — see whilly/adapters/transport/auth.py."
        ),
    )
    parser.add_argument(
        "--plan",
        dest="plan_id",
        default=None,
        help=(
            "Plan id this worker draws claims from. Forwarded verbatim to "
            "whilly-worker after register. Required (no env-var fallback — "
            "the connect flow is a one-shot, an env-var would mask config)."
        ),
    )
    parser.add_argument(
        "--hostname",
        dest="hostname",
        default=None,
        help=("Hostname the new worker self-reports. Defaults to socket.gethostname() if omitted."),
    )
    parser.add_argument(
        "--no-keychain",
        dest="no_keychain",
        action="store_true",
        help=(
            "Do not write the bearer to the OS keychain (or fallback file). "
            "Stdout still prints `worker_id:` / `token:` lines so the operator "
            "can capture the bearer manually."
        ),
    )
    parser.add_argument(
        "--keychain-service",
        dest="keychain_service",
        default=None,
        help=(
            "Override the keychain service name (default: 'whilly'). Mostly a "
            "test seam — operators should leave this alone."
        ),
    )
    parser.add_argument(
        "--insecure",
        dest="insecure",
        action="store_true",
        help=("Allow plain HTTP to a non-loopback control-plane URL. Mirrors the whilly-worker flag of the same name."),
    )
    return parser


def check_opencode_groq_credentials() -> str | None:
    """Return a single-line error message if the operator opted into the
    explicit groq path (``WHILLY_MODEL=groq/...``) but ``GROQ_API_KEY`` is missing.

    Returns ``None`` on success (env is fine, or the operator did not opt
    into the groq path).

    Default since v4.4.2 (feature m1-opencode-big-pickle-default): the
    worker container ships with ``WHILLY_CLI=opencode`` and
    ``WHILLY_MODEL=opencode/big-pickle`` — OpenCode Zen's anonymous
    free-tier model, requiring NO credential. Empty / unset
    ``WHILLY_MODEL`` therefore no longer routes through Groq, so the
    guard must NOT fire on an empty value (zero-key onboarding).

    The check is intentionally narrow:

    * ``WHILLY_CLI`` must be exactly ``opencode`` (case-insensitive,
      whitespace-stripped). Any other CLI selector (or empty/unset)
      returns ``None`` because GROQ_API_KEY is not their concern.
    * ``WHILLY_MODEL`` must start with the literal ``groq/`` prefix —
      i.e. the operator explicitly opted into Groq. Empty value (the
      v4.4.2 default → big-pickle) and any other provider
      (``anthropic/claude-...``, ``openai/gpt-4o``, …) bypass the check.
    * ``GROQ_API_KEY`` is empty / whitespace-only / unset.

    The returned message is a single line so docker-compose / CI
    grep-style assertions can match it without regex acrobatics
    (VAL-M1-AGENT-DEFAULT-002).
    """
    cli = (os.environ.get("WHILLY_CLI") or "").strip().lower()
    if cli != "opencode":
        return None
    model = (os.environ.get("WHILLY_MODEL") or "").strip()
    # Empty WHILLY_MODEL → opencode backend resolves to DEFAULT_MODEL,
    # which is opencode/big-pickle since v4.4.2 (zero-key onboarding).
    # Empty value MUST NOT trigger the groq guard.
    if not model:
        return None
    # provider/... (or provider/sub/...) form. Bare ids without a slash
    # never auto-prefix to ``groq/`` (see _PROVIDER_BY_PREFIX in
    # whilly.agents.opencode), so they cannot reach Groq.
    is_groq = "/" in model and model.split("/", 1)[0].lower() == "groq"
    if not is_groq:
        return None
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if api_key:
        return None
    return (
        "whilly worker: GROQ_API_KEY is required when WHILLY_MODEL=groq/... "
        "(or unset WHILLY_MODEL to use the zero-key opencode/big-pickle default). "
        "See https://console.groq.com to obtain a free key."
    )


def run_connect_command(argv: Sequence[str]) -> int:
    """Execute ``whilly worker connect <url> ...`` — return exit code (M1).

    Returns ``EXIT_OK`` only if ``os.execvp`` is reached; in production
    ``execvp`` replaces the process and never returns, but the value
    still matters for unit tests that monkeypatch ``execvp``.

    Worker-runtime arg pass-through (``--`` sentinel)
    -------------------------------------------------
    ``connect`` itself only understands the connect-CLI args (URL,
    ``--bootstrap-token`` / ``--plan`` / ``--hostname`` / ``--insecure`` /
    ``--no-keychain`` / ``--keychain-service``). Worker-runtime flags
    (``--once``, ``--worker-id``, ``--heartbeat-interval``,
    ``--max-iterations``) belong to the *exec'd* ``whilly-worker``
    binary, not to argparse here. To plumb them through cleanly we treat
    a literal ``--`` token as a sentinel: everything before goes to this
    parser, everything after is appended verbatim to the ``whilly-worker``
    argv right after the connect-supplied ``--connect`` / ``--token`` /
    ``--plan`` triplet (and the optional ``--insecure``).

    This matches the standard POSIX convention (``cmd ARGS -- TARGET-ARGS``)
    and keeps the entrypoint contract honest: the docker
    ``entrypoint.sh worker --once`` invocation lands on the worker loop,
    not on the connect parser.

    Error envelope:

    * ``EXIT_CONNECT_ERROR`` (1) — bootstrap missing/empty, URL invalid,
      scheme guard rejected, register 401, control-plane unreachable,
      ``whilly-worker`` not on PATH after a successful register, or any
      other runtime failure of the connect flow.
    * ``EXIT_ENVIRONMENT_ERROR`` (2) — argparse rejected the invocation
      (e.g. missing positional). Argparse handles this directly via
      ``SystemExit(2)`` when ``parse_args`` fails.
    """
    # Split argv on the first ``--`` token: everything before it is
    # parsed by the connect argparse; everything after it is forwarded
    # verbatim to the exec'd ``whilly-worker`` binary. A trailing bare
    # ``--`` (no args after it) is fine — it just produces an empty
    # passthrough list.
    argv_list = list(argv)
    try:
        sep_idx = argv_list.index("--")
    except ValueError:
        connect_argv: list[str] = argv_list
        worker_passthrough: list[str] = []
    else:
        connect_argv = argv_list[:sep_idx]
        worker_passthrough = argv_list[sep_idx + 1 :]

    parser = build_connect_parser()
    args = parser.parse_args(connect_argv)

    # ── 0. Fail fast on missing GROQ_API_KEY when explicit groq is selected ──
    # Default since v4.4.2 (m1-opencode-big-pickle-default): the worker
    # uses opencode + opencode/big-pickle (zero-key path), so the guard
    # is a no-op for empty/unset WHILLY_MODEL. When the operator opts
    # into Groq via WHILLY_MODEL=groq/..., catch the missing-key case
    # BEFORE any HTTP call so docker compose smoke tests get a clean
    # single-line diagnostic on stderr (VAL-M1-AGENT-DEFAULT-002), and
    # never appear to "register but never claim" because the agent run
    # itself died at the first task with a provider 401.
    groq_err = check_opencode_groq_credentials()
    if groq_err is not None:
        print(groq_err, file=sys.stderr)
        return EXIT_CONNECT_ERROR

    # ── 1. URL validation + scheme guard ─────────────────────────────
    try:
        scheme, host, _port = enforce_scheme_guard(args.control_url, insecure=args.insecure)
    except UrlValidationError as exc:
        print(f"whilly worker connect: {exc}", file=sys.stderr)
        return EXIT_CONNECT_ERROR
    if args.insecure and scheme == "http" and not _is_loopback_host(host):
        print(
            f"whilly worker connect: warning — using plain HTTP to non-loopback host {host!r} "
            "(--insecure). Prefer HTTPS in production.",
            file=sys.stderr,
        )

    # ── 2. Plan id is required (no env-var fallback for this surface). ──
    plan_id_raw = args.plan_id
    if plan_id_raw is None or not plan_id_raw.strip():
        print(
            "whilly worker connect: --plan is required and must be a non-empty plan id (e.g. --plan demo).",
            file=sys.stderr,
        )
        return EXIT_CONNECT_ERROR
    plan_id = plan_id_raw.strip()

    # ── 3. Bootstrap token: --flag > env. Empty / whitespace rejected. ──
    raw_bootstrap = args.bootstrap_token
    if raw_bootstrap is None:
        raw_bootstrap = os.environ.get(BOOTSTRAP_TOKEN_ENV, "")
    bootstrap_token = (raw_bootstrap or "").strip()
    if not bootstrap_token:
        print(
            f"whilly worker connect: --bootstrap-token is required (or set "
            f"{BOOTSTRAP_TOKEN_ENV}); the value must be non-empty after stripping whitespace.",
            file=sys.stderr,
        )
        return EXIT_CONNECT_ERROR

    # ── 4. Hostname (default to socket.gethostname()) ────────────────
    hostname = (args.hostname or socket.gethostname()).strip()
    if not hostname:
        print(
            "whilly worker connect: --hostname must be a non-empty string when supplied.",
            file=sys.stderr,
        )
        return EXIT_CONNECT_ERROR

    # ── 5. Canonical URL (strip trailing slash) for keychain key + worker arg ──
    from whilly.secrets import canonical_control_url

    canonical_url = canonical_control_url(args.control_url.strip())

    # ── 6. Register against the control plane ───────────────────────
    try:
        register_response = asyncio.run(
            _async_register(canonical_url, bootstrap_token, hostname),
        )
    except KeyboardInterrupt:
        # Re-raise so the shell sees the canonical 130 exit; the
        # contract (VAL-M1-CONNECT-906) only asks that no half-state
        # is persisted and the keyring is not touched on the early-exit
        # path — both are guaranteed because we have not stored anything
        # yet.
        raise
    except Exception as exc:
        # Map well-known transport failures to readable diagnostics. We
        # import lazily to avoid pulling httpx into ``whilly --help``
        # cold-starts that don't need it.
        from whilly.adapters.transport.client import (
            AuthError,
            HTTPClientError,
            ServerError,
        )

        if isinstance(exc, AuthError):
            print(
                f"whilly worker connect: control-plane rejected bootstrap token (401) for {canonical_url}. "
                "Check the value passed via --bootstrap-token / "
                f"{BOOTSTRAP_TOKEN_ENV} and that it has not been revoked.",
                file=sys.stderr,
            )
        elif isinstance(exc, ServerError):
            # ``ServerError`` is a subclass of ``HTTPClientError``, so the
            # check has to come *before* the generic-HTTP branch below.
            print(
                f"whilly worker connect: control-plane server error for {canonical_url} after retries: {exc}",
                file=sys.stderr,
            )
        elif isinstance(exc, HTTPClientError):
            print(
                f"whilly worker connect: control-plane returned HTTP error for {canonical_url}: {exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"whilly worker connect: control-plane unreachable at {canonical_url}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return EXIT_CONNECT_ERROR

    worker_id = register_response.worker_id
    bearer = register_response.token

    # ── 7. Persist the bearer (keychain or file fallback) ────────────
    storage_backend: str | None = None
    if not args.no_keychain:
        try:
            from whilly.secrets import WHILLY_KEYRING_SERVICE, store_worker_credential

            storage_backend = store_worker_credential(
                canonical_url,
                bearer,
                service=args.keychain_service or WHILLY_KEYRING_SERVICE,
            )
        except Exception as exc:
            # The file-fallback is the catch-all; only a hard disk-write
            # failure (permission, no space) gets here. Surface it on
            # stderr but do NOT swallow the exec — the operator already
            # has the plaintext on stdout below and can re-store later.
            print(
                f"whilly worker connect: warning — failed to persist bearer to keychain or fallback file: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            storage_backend = None

    # ── 8. Print stdout: ``worker_id:`` / ``token:`` (line-oriented, pipeable). ──
    # IMPORTANT: no banners between the two lines (VAL-M1-CONNECT-007).
    sys.stdout.write(f"worker_id: {worker_id}\n")
    sys.stdout.write(f"token: {bearer}\n")
    sys.stdout.flush()

    # Diagnostic line (post-token) on STDERR — never includes the
    # plaintext bearer (VAL-M1-CONNECT-912 / 913).
    if storage_backend == "keyring":
        print(
            f"whilly worker connect: bearer stored in OS keychain (service='whilly', user={canonical_url}).",
            file=sys.stderr,
        )
    elif storage_backend == "file":
        from whilly.secrets import credentials_file_path

        print(
            f"whilly worker connect: bearer stored in fallback file {credentials_file_path()} (mode 0600).",
            file=sys.stderr,
        )
    elif args.no_keychain:
        print(
            "whilly worker connect: --no-keychain set; bearer printed to stdout only.",
            file=sys.stderr,
        )

    # ── 9. Exec into ``whilly-worker``. ──────────────────────────────
    # ``execvp`` replaces the current process — on success it never
    # returns. We pass the bearer via argv (matching the long-running
    # contract; the bearer is already ephemeral and execvp argv is
    # process-private). If ``whilly-worker`` is missing from PATH,
    # ``execvp`` raises ``FileNotFoundError`` — surface it cleanly.
    #
    # ``--worker-id <minted_id>`` is REQUIRED here even though
    # ``whilly-worker`` would otherwise auto-generate one. The bearer
    # token returned by ``POST /workers/register`` is bound (via
    # ``token_hash``) to the *registered* ``worker_id``; if we let
    # ``whilly-worker`` mint a fresh id at startup, every subsequent
    # ``/tasks/claim`` / ``/workers/<id>/heartbeat`` would carry the
    # new id while the bearer authenticated as the registered id — the
    # ``_require_token_owner`` route helper would reject the mismatch
    # with 403. Pinning the registered id makes the exec'd worker speak
    # the same identity the server already issued credentials for, so
    # claim/heartbeat round-trips return 200 instead of 403. (M1
    # blocking finding: VAL-M1-CONNECT-008 / VAL-M1-CONNECT-021 /
    # VAL-M1-ENTRYPOINT-002.)
    worker_argv = [
        "whilly-worker",
        "--connect",
        canonical_url,
        "--token",
        bearer,
        "--plan",
        plan_id,
        "--worker-id",
        worker_id,
    ]
    if args.insecure:
        worker_argv.append("--insecure")
    # Worker-runtime args from the ``--`` sentinel (e.g. ``--once``,
    # ``--worker-id X``, ``--heartbeat-interval 5``) come last so that
    # the operator can override anything we set above (argparse keeps
    # the *last* occurrence on the worker side). This is the only path
    # that reaches the worker loop's argparse — connect's own argparse
    # never sees these flags.
    worker_argv.extend(worker_passthrough)
    try:
        os.execvp(worker_argv[0], worker_argv)
    except FileNotFoundError:
        print(
            "whilly worker connect: registration succeeded and the bearer was persisted, "
            "but the 'whilly-worker' executable was not found on PATH. Install the worker "
            "package and re-run: whilly-worker --connect <url> --token <bearer> --plan <id>.",
            file=sys.stderr,
        )
        return EXIT_CONNECT_ERROR
    # ``execvp`` succeeded — unreachable in production, but keeps mypy /
    # call-sites that monkeypatch the exec happy.
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point — registered as ``whilly-worker`` in pyproject.toml.

    Thin wrapper over :func:`run_worker_command` that resolves
    ``argv`` from :data:`sys.argv` when called as the binary. Tests
    invoke :func:`run_worker_command` directly with a list to avoid
    poking at ``sys.argv``.

    The wrapper also dispatches the ``register`` and ``connect``
    subcommands to their handlers when the first positional token is
    ``"register"`` / ``"connect"`` — keeps the standalone
    ``whilly-worker`` console script consistent with the
    ``whilly worker <sub>`` flow invoked through the dispatcher in
    :mod:`whilly.cli`.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] == "register":
        return run_register_command(args[1:])
    if args and args[0] == "connect":
        return run_connect_command(args[1:])
    return run_worker_command(args)


# ``python -m whilly.cli.worker`` entry point. Mirrors :mod:`whilly.__main__`
# / :mod:`whilly.cli.__main__` so a test that needs to spawn the worker as
# a subprocess can do so via ``sys.executable -m whilly.cli.worker``
# without depending on a ``whilly-worker`` console script being on
# ``$PATH``. Specifically robust to PATH pollution from stale pipx
# installs that frequently shadow the active venv's ``whilly-worker``
# (see ``tests/integration/test_phase5_remote.py`` for the exact
# observed failure mode).
if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
