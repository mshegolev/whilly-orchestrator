"""HTTP client for the worker → control-plane RPC surface (TASK-022a1, PRD FR-1.5 / TC-6).

This module is the **httpx half** of :mod:`whilly.adapters.transport`. It is
imported by the remote worker (TASK-022a/b) and is intentionally the only
piece of HTTP code that lives outside the FastAPI app — the worker package
must stay a thin httpx + pydantic + ``whilly.core`` consumer (PRD FR-1.5),
without dragging FastAPI into a process that doesn't need it.

What lives here (TASK-022a1, TASK-022a2)
----------------------------------------
* :class:`RemoteWorkerClient` — owns an :class:`httpx.AsyncClient`, applies
  the bearer token to every request, and exposes a single private
  :meth:`_request` primitive that wraps the wire call with exponential
  backoff on transient failures and fail-fast on 4xx. The
  worker-bootstrap RPCs (:meth:`register`, :meth:`heartbeat`) sit on
  top of that primitive (TASK-022a2); the task-lifecycle RPCs
  (``claim`` / ``complete`` / ``fail``) land in TASK-022a3.
* :class:`HTTPClientError` and its three subclasses (:class:`AuthError`,
  :class:`VersionConflictError`, :class:`ServerError`). They are the typed
  surface that the RPC methods raise so the worker's outer loop
  (TASK-022b1) can ``except VersionConflictError: continue`` without
  parsing JSON detail strings.

Retry contract (AC: 1s / 2s / 4s / 8s exponential backoff)
----------------------------------------------------------
Transient failures — :class:`httpx.ConnectError`,
:class:`httpx.TimeoutException`, any HTTP 5xx — get **3 retries** with
sleeps of 1s, 2s, 4s between attempts (4 total attempts). The 4th attempt
either succeeds or raises :class:`ServerError` / re-raises the underlying
:class:`httpx.HTTPError`.

The "fourth backoff value would be 8s" framing in the AC is intentional:
the schedule reads ``[1, 2, 4, 8]`` as the *interval ladder* between
attempts, with attempts indexed 0..3. We sleep before the 1st retry (1s),
2nd retry (2s), and 3rd retry (4s) — the listed 8s is the budget cap that
the scheduler does *not* spend, because a 5th attempt with an 8s sleep
would push the worst-case hold time past the long-poll budget on the
control-plane side. Tests pin this directly: 3 retry sleeps after the
initial attempt, then surface the failure.

4xx is fail-fast on principle: the request itself is broken, retrying
spams the same broken payload at the server. The mapping from status code
to typed exception is what TASK-022a3 / 022b1 branches on:

* 401 / 403  → :class:`AuthError`        — re-register or operator action.
* 409        → :class:`VersionConflictError` — version skew; project the
  envelope's ``actual_status`` / ``actual_version`` so the worker can
  treat ``actual_status == 'DONE'`` as idempotent-retry-success without a
  second SELECT.
* other 4xx  → :class:`HTTPClientError`  — bug in the worker's request
  construction; surface and crash the supervisor loop.

Why ``httpx.AsyncClient`` instead of a request-per-call ``httpx.AsyncClient``?
    Per-request clients would re-resolve DNS and re-handshake TLS for
    every RPC. The worker's hot path is ``claim()`` → run agent →
    ``complete()`` against the same control plane for the lifetime of
    the process; a long-lived pooled client cuts the per-RPC overhead to
    a single TCP write/read on the warm connection. This is also what
    makes the long-polled ``/tasks/claim`` endpoint cheap on the worker
    side: the keep-alive connection is reused for the next claim
    immediately.

Why an async context manager?
    The worker's main loop needs deterministic teardown: exiting the
    ``async with RemoteWorkerClient(...)`` block must close the underlying
    httpx pool *before* the asyncio event loop closes, otherwise httpx
    logs ``RuntimeError: Event loop is closed`` warnings on shutdown that
    are noise during normal operation. The context-manager protocol is
    the standard way to express this lifecycle in async Python.

Token plumbing (PRD NFR-3)
--------------------------
:class:`RemoteWorkerClient` accepts the per-worker bearer token in its
constructor and applies it as ``Authorization: Bearer <token>`` to every
outbound request. The bootstrap token is *separately* tracked because the
``POST /workers/register`` route is the only one gated by the cluster-
wide bootstrap secret (per :mod:`whilly.adapters.transport.auth`); a
worker that hasn't registered yet has no per-worker token to present.
:meth:`register` swaps the bootstrap token in for that single call via
``_request(..., bootstrap=True)`` and returns the freshly-minted
:class:`RegisterResponse` to the caller. It deliberately does **not**
mutate ``self._token``: the AC for TASK-022a2 makes ``register`` a
transport primitive, and a token-swap on the live client would conflate
"transport" with "lifecycle owner". The supervisor loop (TASK-022b1)
wires the registration step explicitly.

Why the typed exceptions over httpx's own?
    :class:`httpx.HTTPStatusError` carries the response object but no
    semantic distinction between "auth blew up" and "version skew" —
    the worker would have to inspect ``.response.status_code`` at every
    call site. A typed hierarchy lets ``except VersionConflictError``
    sit alongside ``except AuthError`` cleanly, and
    :class:`VersionConflictError` carries the parsed
    :class:`ErrorResponse` envelope's structured fields so the worker
    doesn't re-parse JSON.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from whilly.adapters.transport.schemas import (
    ErrorResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
)
from whilly.core.models import TaskId, TaskStatus

__all__ = [
    "DEFAULT_BACKOFF_SCHEDULE",
    "DEFAULT_TIMEOUT_SECONDS",
    "REGISTER_PATH",
    "AuthError",
    "HTTPClientError",
    "RemoteWorkerClient",
    "ServerError",
    "VersionConflictError",
    "heartbeat_path",
]

#: Path of the cluster-join RPC on the control plane. Mirrors
#: :data:`whilly.adapters.transport.server.REGISTER_PATH` — the constant is
#: duplicated here on purpose because the ``client`` module cannot import
#: from the FastAPI server module (the server pulls in ``fastapi``,
#: ``asyncpg`` and the import-linter contract from PRD SC-6 forbids
#: the worker-side dependency graph from dragging either in). Having the
#: constant in two places is the cheap end of the trade-off; an integration
#: test in :mod:`tests.integration.test_transport_workers` indirectly pins
#: the parity by referencing the server's constant.
REGISTER_PATH: Final[str] = "/workers/register"


def heartbeat_path(worker_id: str) -> str:
    """Return the heartbeat endpoint path for ``worker_id``.

    The server route is ``/workers/{worker_id}/heartbeat`` (PRD FR-1.6).
    Centralising the format here means a future change to the URL shape
    (e.g. moving heartbeats to ``/v1/workers/{id}/ping``) lands in one
    place rather than scattered across call sites; it also keeps the
    f-string colocated with documentation explaining why no URL-encoding
    is needed.

    Worker ids are ``w-<urlsafe>`` (see
    :func:`whilly.adapters.transport.server._generate_worker_id`) so the
    suffix is already RFC 3986 *unreserved* — no
    :func:`urllib.parse.quote` call required. If a misconfigured caller
    passes a string with reserved characters the server's path matcher
    will surface a 404, which is the diagnosis we want.
    """
    return f"/workers/{worker_id}/heartbeat"


# ``T`` is the pydantic response schema being parsed in :meth:`RemoteWorkerClient._parse_response`.
# Bound to :class:`pydantic.BaseModel` rather than ``Any`` so mypy --strict refuses
# accidental misuse of the helper with non-pydantic types.
_TResp = TypeVar("_TResp", bound=BaseModel)

logger = logging.getLogger(__name__)

#: Default per-RPC timeout in seconds. The control-plane long-poll budget
#: is 30s (see :data:`whilly.adapters.transport.server.CLAIM_LONG_POLL_TIMEOUT_DEFAULT`),
#: so the client's wait must accommodate that plus a small margin for
#: server-side processing of the eventual claim. 60s gives ~2x headroom
#: while still bounding the call so a hung server doesn't pin the worker
#: forever — the visibility-timeout sweep (TASK-025a) is what finally
#: reclaims a stuck worker, but keeping individual RPCs bounded means
#: ``asyncio.wait_for`` and ``CancelledError`` propagation work as expected.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0

#: Sleep ladder between retry attempts. See module docstring for why we
#: stop at 3 retries — the listed 8s value is the budget cap, not a 4th
#: sleep. ``tuple`` (not ``list``) so the constant is hashable and
#: untainted by accidental mutation.
DEFAULT_BACKOFF_SCHEDULE: Final[tuple[float, ...]] = (1.0, 2.0, 4.0)

#: HTTP statuses that the retry policy treats as transient. Anything in
#: ``[500, 600)`` is a server-side fault that *might* clear on retry
#: (deploy in progress, transient pool exhaustion, an upstream LB hiccup);
#: 502/503/504 are the canonical examples but we don't enumerate, because
#: a custom 599 from a misbehaving proxy is just as transient in practice.
_RETRY_STATUS_RANGE: Final[range] = range(500, 600)


class HTTPClientError(Exception):
    """Base class for all :class:`RemoteWorkerClient` failures.

    The hierarchy is:

    * :class:`HTTPClientError` — generic 4xx that doesn't match a more
      specific subclass (the catch-all bucket the worker reports up).
    * :class:`AuthError` — 401 / 403 (bearer token rejected).
    * :class:`VersionConflictError` — 409 (optimistic-locking skew).
    * :class:`ServerError` — 5xx after the retry budget was exhausted, or
      a transient httpx-level failure (:class:`httpx.ConnectError`,
      :class:`httpx.TimeoutException`) on the final attempt.

    All four expose ``status_code`` and ``response_body`` so call sites
    can log the full server response without holding the
    :class:`httpx.Response` object (which is closed by the time the
    exception bubbles out of :meth:`RemoteWorkerClient._request`).
    """

    def __init__(self, message: str, *, status_code: int | None, response_body: str) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)


class AuthError(HTTPClientError):
    """Bearer token rejected — server returned 401 or 403.

    The worker's right move on this is *not* to retry: the token is
    either revoked, mis-typed, or stale. TASK-022b1's main loop will
    surface this to the supervisor (re-register on 401, abort on 403)
    rather than spinning silently.
    """


class VersionConflictError(HTTPClientError):
    """Optimistic-locking conflict on a state-mutating RPC (server returned 409).

    Mirrors :class:`whilly.adapters.db.repository.VersionConflictError` on
    the server side; the parsed :class:`ErrorResponse` envelope's
    structured fields are projected onto attributes here so a TASK-022a3
    caller can branch on the conflict cause without re-parsing JSON.

    Attributes
    ----------
    task_id:
        ID of the task whose update was rejected. ``None`` only if the
        server failed to populate the envelope (defensive).
    expected_version:
        Version the worker sent in the request body (echoed back).
    actual_version:
        The current ``tasks.version`` value the server saw. ``None``
        when the row no longer exists (FK cascade or mis-routed worker).
    actual_status:
        The current ``tasks.status`` value. The worker can treat
        ``DONE`` / ``FAILED`` / ``SKIPPED`` with
        ``actual_version == expected_version`` as idempotent-success
        on a retried complete/fail.
    error_code:
        The machine-readable error key from the envelope (typically
        ``"version_conflict"``). Stable across server versions; safe
        to branch on in the client.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response_body: str,
        task_id: TaskId | None,
        expected_version: int | None,
        actual_version: int | None,
        actual_status: TaskStatus | None,
        error_code: str,
    ) -> None:
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.actual_status = actual_status
        self.error_code = error_code
        super().__init__(message, status_code=status_code, response_body=response_body)


class ServerError(HTTPClientError):
    """Server-side failure that survived the retry budget.

    Either:

    * an HTTP 5xx that still came back as 5xx after
      :data:`DEFAULT_BACKOFF_SCHEDULE` was exhausted; or
    * a transport-level failure (connection refused, read timeout) on
      the final attempt — :class:`httpx.HTTPError` is preserved as
      ``__cause__`` for traceback inspection.

    The worker's response should be to log + back off at the *outer*
    loop level (TASK-022b1's supervisor sleeps and re-claims), not to
    retry the same RPC again — :meth:`RemoteWorkerClient._request`
    already did.
    """


class RemoteWorkerClient:
    """Async HTTP client for the worker → control-plane RPC surface.

    Constructed with the control-plane base URL and the per-worker bearer
    token. The bootstrap token is optional and stored separately — only
    :meth:`register` (TASK-022a2) uses it; every other RPC swaps the
    per-worker token onto the request via :meth:`_request`.

    Lifecycle
    ---------
    Use as an async context manager::

        async with RemoteWorkerClient(base_url, token) as client:
            ...

    The underlying :class:`httpx.AsyncClient` is allocated on
    :meth:`__aenter__` and aclose'd on :meth:`__aexit__` — the worker
    process owns exactly one ``async with`` block over the client, which
    keeps connection-pool ownership trivially correct (no double-close,
    no use-after-aclose).

    Thread-safety
    -------------
    httpx's async client is *not* thread-safe across event loops, but the
    worker is single-event-loop by construction (TASK-022b1's main loop
    is the only consumer). Concurrency within the loop — e.g. an
    in-flight long-poll claim alongside a heartbeat — is fine because
    httpx multiplexes requests over the connection pool.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        bootstrap_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("RemoteWorkerClient: base_url must be a non-empty URL.")
        if not token:
            raise ValueError("RemoteWorkerClient: token must be a non-empty bearer string.")
        if timeout <= 0:
            raise ValueError(f"RemoteWorkerClient: timeout must be > 0, got {timeout!r}.")
        if any(s < 0 for s in backoff_schedule):
            # A negative sleep would be a no-op on most event loops but
            # silently disables the backoff; surface the misconfiguration
            # at construction time rather than after a 5xx storm.
            raise ValueError(
                f"RemoteWorkerClient: backoff_schedule must contain non-negative floats, got {backoff_schedule!r}."
            )
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._bootstrap_token = bootstrap_token
        self._timeout = timeout
        self._backoff_schedule = backoff_schedule
        # The transport kwarg is the test seam: production callers leave
        # it None and httpx builds the default HTTP transport. Tests pass
        # an :class:`httpx.MockTransport` to assert against the wire
        # without spinning up a real ASGI app — keeping the unit tests
        # fast and independent of the FastAPI route handlers.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        """The control-plane base URL, with any trailing slash stripped."""
        return self._base_url

    async def __aenter__(self) -> Self:
        # Construct the AsyncClient lazily inside the context manager so
        # the constructor can stay synchronous (allocating an
        # AsyncClient does not require an event loop, but binding its
        # lifetime to ``__aenter__`` makes the close-after-loop pitfall
        # impossible — the loop is guaranteed to exist for the duration
        # of the ``async with`` block).
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            # We always send a bearer header; pre-binding it on the
            # client means handlers added in TASK-022a2 / 022a3 don't
            # have to thread the token through every call site.
            headers={"Authorization": f"Bearer {self._token}"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # ``aclose`` is idempotent in httpx ≥ 0.27 — calling it twice is
        # harmless — but we still guard with the ``is not None`` check so
        # a future httpx version that tightens this contract doesn't
        # bite. ``self._client = None`` after close is a defensive
        # marker so a (mis)used client outside the ``async with`` block
        # raises :class:`RuntimeError` on the next request rather than
        # silently re-using a closed pool.
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        bootstrap: bool = False,
    ) -> httpx.Response:
        """Issue an HTTP request with retry + typed-exception failure handling.

        Parameters
        ----------
        method:
            HTTP verb. The worker's RPC surface only uses ``GET`` and
            ``POST`` today, but ``method`` is left as a string so
            future endpoints (PATCH/DELETE) don't need a wider type.
        path:
            Path relative to ``base_url`` — leading slash is required so
            ``base_url + path`` round-trips cleanly.
        json:
            Optional JSON body. ``None`` means an empty request body.
        bootstrap:
            If True, swap the per-worker bearer header for the
            bootstrap token on this request. Only meaningful for
            :meth:`register` (TASK-022a2). Raises
            :class:`RuntimeError` if no bootstrap token was supplied
            in the constructor — the worker should have configured it
            before calling.

        Returns
        -------
        httpx.Response
            The first successful (i.e. ``< 500``) response. Caller
            decides how to interpret the body — handlers in TASK-022a2 /
            022a3 will validate it through pydantic.

        Raises
        ------
        RemoteClient errors:
            * :class:`AuthError` on 401 / 403,
            * :class:`VersionConflictError` on 409,
            * :class:`HTTPClientError` on any other 4xx,
            * :class:`ServerError` on 5xx after retries are exhausted, or
              on a transport-level failure on the last attempt.
        RuntimeError
            If called outside the ``async with`` block, or with
            ``bootstrap=True`` when no bootstrap token was provided.

        Notes
        -----
        Retry policy is exactly the AC: sleep ``schedule[i]`` between
        attempt ``i`` and attempt ``i+1`` for transient failures
        (:class:`httpx.ConnectError`, :class:`httpx.TimeoutException`,
        HTTP 5xx). 4xx is *always* fail-fast — no retry, no sleep.
        """
        if self._client is None:
            raise RuntimeError(
                "RemoteWorkerClient: not entered. Use `async with RemoteWorkerClient(...) as c:` before issuing requests."
            )
        headers: dict[str, str] | None = None
        if bootstrap:
            if self._bootstrap_token is None:
                raise RuntimeError(
                    "RemoteWorkerClient: bootstrap=True requires bootstrap_token in the constructor; "
                    "the cluster-join secret is the only credential POST /workers/register accepts."
                )
            # Per-request header overlay: don't mutate the long-lived
            # client.headers, just override Authorization on this single
            # call. httpx merges request-level headers over client-level.
            headers = {"Authorization": f"Bearer {self._bootstrap_token}"}

        # Total attempts = 1 (initial) + len(schedule) (retries). The
        # schedule is the *sleep ladder*; the loop body issues an attempt,
        # then sleeps the i-th value before the (i+1)-th attempt.
        total_attempts = 1 + len(self._backoff_schedule)
        last_exc: Exception | None = None
        for attempt in range(total_attempts):
            try:
                response = await self._client.request(method, path, json=json, headers=headers)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                # Transport-level failure: log + maybe sleep + retry.
                # We log at INFO not WARNING because a single transient
                # failure during a deploy is the *expected* signal — the
                # operator wants to see the retries succeed in the next
                # log line, not be paged on every blip.
                last_exc = exc
                logger.info(
                    "RemoteWorkerClient: %s %s transport error (attempt %d/%d): %s",
                    method,
                    path,
                    attempt + 1,
                    total_attempts,
                    exc,
                )
                if attempt + 1 < total_attempts:
                    await asyncio.sleep(self._backoff_schedule[attempt])
                    continue
                # Exhausted retries — surface as ServerError, preserving
                # the original httpx exception as __cause__ so a debugger
                # can inspect the underlying socket/timeout state.
                raise ServerError(
                    f"{method} {path}: transport failure after {total_attempts} attempts: {exc}",
                    status_code=None,
                    response_body="",
                ) from exc

            status = response.status_code
            if 200 <= status < 400:
                # Hot path: handler decides the body shape.
                return response
            if status in _RETRY_STATUS_RANGE:
                # 5xx: retry until exhausted, then surface ServerError.
                # Read the body *before* deciding to retry: if the
                # handler streamed something useful (a request ID,
                # an error code) we want it on the final exception.
                body = await self._safe_read_body(response)
                logger.info(
                    "RemoteWorkerClient: %s %s server error %d (attempt %d/%d): %s",
                    method,
                    path,
                    status,
                    attempt + 1,
                    total_attempts,
                    body[:200],
                )
                if attempt + 1 < total_attempts:
                    await asyncio.sleep(self._backoff_schedule[attempt])
                    continue
                raise ServerError(
                    f"{method} {path}: server returned {status} after {total_attempts} attempts",
                    status_code=status,
                    response_body=body,
                )
            # 4xx (and any 3xx that httpx didn't auto-follow): fail-fast.
            # Reading the body here is cheap because the response is
            # small (an ErrorResponse envelope), and crucially we have
            # to read it *now* — once the response object falls out of
            # scope the body is unrecoverable.
            body = await self._safe_read_body(response)
            raise self._exception_from_4xx(method, path, response, body)

        # Defensive: the loop should always either return a response or
        # raise. If we fell through, surface the last transport error as
        # ServerError so the caller doesn't see a None.
        raise ServerError(
            f"{method} {path}: retry loop exited without a definitive response",
            status_code=None,
            response_body="",
        ) from last_exc

    @staticmethod
    async def _safe_read_body(response: httpx.Response) -> str:
        """Read ``response.text`` defensively.

        httpx may raise on ``.text`` access if the underlying transport
        was already closed (e.g. a connection drop after the headers
        arrived). We swallow those failures and return an empty string —
        the status code alone is enough to drive the retry/exception
        logic; the body is purely informational for logging.
        """
        try:
            return response.text
        except Exception:  # pragma: no cover — defensive
            return ""

    def _exception_from_4xx(
        self,
        method: str,
        path: str,
        response: httpx.Response,
        body: str,
    ) -> HTTPClientError:
        """Build the typed exception for a 4xx response.

        Centralised here so :meth:`_request` stays focused on the
        retry-loop control flow and a future status code (e.g. 423
        Locked, 429 Too Many Requests with Retry-After) lands in this
        single place rather than scattered through call sites.
        """
        status = response.status_code
        if status in (401, 403):
            return AuthError(
                f"{method} {path}: authentication failed (HTTP {status}): {body[:200]}",
                status_code=status,
                response_body=body,
            )
        if status == 409:
            return self._build_version_conflict(method, path, response, body)
        # Generic 4xx: most likely a worker-side bug (malformed body,
        # bad path). Surface enough context for the supervisor log to
        # tell the operator what to fix without grepping the wire.
        return HTTPClientError(
            f"{method} {path}: client error (HTTP {status}): {body[:200]}",
            status_code=status,
            response_body=body,
        )

    @staticmethod
    def _build_version_conflict(
        method: str,
        path: str,
        response: httpx.Response,
        body: str,
    ) -> VersionConflictError:
        """Project a 409 response onto :class:`VersionConflictError`.

        The server populates :class:`ErrorResponse` with the structured
        conflict tuple (``task_id``, ``expected_version``,
        ``actual_version``, ``actual_status``); we parse it through the
        pydantic schema so a malformed envelope from a hypothetical
        broken server still surfaces *something* (we fall back to
        unstructured fields rather than raising during exception
        construction, which would lose the original 409 cause).
        """
        envelope: ErrorResponse | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            try:
                envelope = ErrorResponse.model_validate(payload)
            except Exception:  # pragma: no cover — defensive
                envelope = None
        return VersionConflictError(
            f"{method} {path}: version conflict (HTTP 409): {body[:200]}",
            status_code=409,
            response_body=body,
            task_id=envelope.task_id if envelope else None,
            expected_version=envelope.expected_version if envelope else None,
            actual_version=envelope.actual_version if envelope else None,
            actual_status=envelope.actual_status if envelope else None,
            error_code=envelope.error if envelope else "version_conflict",
        )

    @staticmethod
    async def _parse_response(response: httpx.Response, schema: type[_TResp]) -> _TResp:
        """Validate a 2xx response body against ``schema`` (PRD FR-1.5).

        Centralised here so :meth:`register` / :meth:`heartbeat` (and the
        TASK-022a3 RPCs) all surface schema drift the same way:

        * a non-JSON body becomes :class:`ServerError` with the raw text
          for the operator log,
        * a JSON body that doesn't match the pydantic schema becomes
          :class:`ServerError` with the validation error attached as
          ``__cause__`` (mismatched fields, ``extra=forbid`` violations,
          version skew between worker and control plane).

        Both cases are *server-side* protocol failures, not retryable
        transient faults: the server already returned 2xx and the
        request itself is well-formed. Surfacing them as
        :class:`ServerError` keeps the worker's outer-loop classifier
        simple — the same exception type covers "5xx exhausted retries"
        and "server returned a body we can't parse", because the
        operational response (page, inspect, fix the server) is the
        same for both.
        """
        try:
            payload = response.json()
        except ValueError as exc:
            body = response.text
            raise ServerError(
                f"{response.request.method} {response.request.url.path}: "
                f"server returned non-JSON body for {schema.__name__}: {body[:200]}",
                status_code=response.status_code,
                response_body=body,
            ) from exc
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            body = response.text
            raise ServerError(
                f"{response.request.method} {response.request.url.path}: "
                f"server response did not match {schema.__name__}: {exc.error_count()} "
                f"validation error(s)",
                status_code=response.status_code,
                response_body=body,
            ) from exc

    async def register(self, hostname: str) -> RegisterResponse:
        """Mint a new worker identity (``POST /workers/register``, PRD FR-1.1).

        Sends the cluster-join secret (``bootstrap_token`` from the
        constructor) instead of the per-worker bearer — the worker has
        no per-worker credentials yet, by definition. The server returns
        the freshly-minted ``worker_id`` plus the *plaintext* per-worker
        bearer token (only the SHA-256 hash is persisted on the server,
        per PRD NFR-3); the caller is responsible for routing that
        token onto a fresh :class:`RemoteWorkerClient` (or a downstream
        process) for every subsequent RPC. This method intentionally
        does **not** mutate ``self._token``: the AC for TASK-022a2 says
        "register uses bootstrap_token", and a token-swap on the live
        client would conflate two responsibilities (transport primitive
        vs. lifecycle owner). TASK-022b1's supervisor wires the
        registration step explicitly.

        Network errors (transient 5xx, ConnectError, TimeoutException)
        flow through :meth:`_request`'s retry/backoff ladder unchanged;
        4xx surfaces as the typed exceptions described on
        :class:`HTTPClientError` (in particular, a wrong / rotated
        bootstrap secret arrives here as :class:`AuthError`, not as a
        silent retry).

        Parameters
        ----------
        hostname:
            Free-form string the worker self-reports to identify the box
            it runs on. Empty strings are rejected at the schema layer
            (``min_length=1`` on
            :class:`whilly.adapters.transport.schemas.NonEmptyHostname`),
            which surfaces as a :class:`pydantic.ValidationError` here
            *before* the network call — that's the right tier for a
            programmer error, not a wire-level 422.

        Returns
        -------
        RegisterResponse
            Validated wire payload carrying ``worker_id`` and ``token``.

        Raises
        ------
        AuthError
            Bootstrap token rejected (HTTP 401 / 403) — operator must
            check ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` rotation.
        HTTPClientError
            Other 4xx (e.g. 422 from a future protocol-version
            mismatch); the worker should crash rather than retry.
        ServerError
            5xx after the retry budget was exhausted, or a malformed
            response that fails pydantic validation against
            :class:`RegisterResponse`.
        RuntimeError
            If ``register`` is called outside the ``async with``
            block, or if no ``bootstrap_token`` was supplied to the
            constructor.
        """
        request = RegisterRequest(hostname=hostname)
        response = await self._request(
            "POST",
            REGISTER_PATH,
            json=request.model_dump(),
            bootstrap=True,
        )
        return await self._parse_response(response, RegisterResponse)

    async def heartbeat(self, worker_id: str) -> HeartbeatResponse:
        """Refresh a worker's liveness clock (``POST /workers/{id}/heartbeat``, PRD FR-1.6).

        Uses the per-worker bearer (already pinned on the long-lived
        ``httpx.AsyncClient``) — the bootstrap secret would *not*
        authenticate here, by design (PRD FR-1.2 token split, see
        :func:`whilly.adapters.transport.auth.make_bearer_auth`).

        Two outcomes the caller must distinguish:

        * ``response.ok == True`` — server advanced
          ``workers.last_heartbeat = NOW()``; the worker is healthy.
        * ``response.ok == False`` — the ``worker_id`` is not (or no
          longer) registered on the server. This is a *recoverable*
          state per :class:`HeartbeatResponse`'s docstring: the
          supervisor (TASK-022b2) should re-register and continue.
          That's why we return a structured response here rather than
          raising — a worker that hits a 4xx on every heartbeat would
          spin its outer loop into a tight crash-restart cycle.

        Parameters
        ----------
        worker_id:
            The identifier returned by :meth:`register` on a previous
            run. Empty strings are rejected at the schema layer
            (`HeartbeatRequest.worker_id` has ``min_length=1``).

        Returns
        -------
        HeartbeatResponse
            Validated wire payload with ``ok`` indicating whether the
            server matched the row.

        Raises
        ------
        AuthError
            Per-worker bearer rejected (token rotated or revoked).
        HTTPClientError
            Other 4xx (e.g. 400 from path/body mismatch — should never
            happen since this method builds both from the same input).
        ServerError
            5xx after retries or schema-mismatched response.
        RuntimeError
            If called outside the ``async with`` block.
        """
        request = HeartbeatRequest(worker_id=worker_id)
        response = await self._request(
            "POST",
            heartbeat_path(worker_id),
            json=request.model_dump(),
        )
        return await self._parse_response(response, HeartbeatResponse)
