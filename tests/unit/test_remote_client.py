"""Unit tests for :mod:`whilly.adapters.transport.client` (TASK-022a1, PRD FR-1.5 / TC-6).

The :class:`RemoteWorkerClient` is the worker process's only HTTP touch
point with the control plane, so the retry policy and the 4xx → typed-
exception mapping in :meth:`RemoteWorkerClient._request` are load-bearing.
These tests pin the AC for TASK-022a1 directly:

* ``__aenter__`` / ``__aexit__`` allocate and dispose the underlying
  :class:`httpx.AsyncClient`;
* the bearer token from the constructor lands as ``Authorization: Bearer
  <token>`` on every outbound request, *and* a ``bootstrap=True`` request
  swaps in the bootstrap token instead;
* ``_request`` retries on :class:`httpx.ConnectError`,
  :class:`httpx.TimeoutException`, and any HTTP 5xx — sleeping the
  documented 1s/2s/4s ladder between attempts (we patch ``asyncio.sleep``
  to assert on the schedule without slowing the suite);
* 4xx responses are *fail-fast* — no retry, no sleep — and surface as the
  documented typed exceptions (:class:`AuthError` / 401·403,
  :class:`VersionConflictError` / 409,
  :class:`HTTPClientError` / other 4xx);
* a 409 response carrying an :class:`ErrorResponse` envelope projects the
  structured fields onto :class:`VersionConflictError` so callers can
  branch on ``actual_status`` directly.

These tests use :class:`httpx.MockTransport` rather than spinning up a
real ASGI app so they stay in the "unit" tier (sub-second) and do not
require Docker / asyncpg / FastAPI to be wired up.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from whilly.adapters.transport.client import (
    DEFAULT_BACKOFF_SCHEDULE,
    AuthError,
    HTTPClientError,
    RemoteWorkerClient,
    ServerError,
    VersionConflictError,
)
from whilly.adapters.transport.schemas import ErrorResponse
from whilly.core.models import TaskStatus

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_sleeps(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Replace ``asyncio.sleep`` with a no-op that records the requested duration.

    The retry ladder under test sleeps real seconds; in production that's
    the correct behaviour but in unit tests it would balloon the suite
    runtime. Patching the function lets us assert on the *schedule* directly
    (e.g. ``[1.0, 2.0, 4.0]``) without waiting 7s wall-clock per test.

    Yielded list grows in sleep-call order so a test can also assert on
    the call count (``len(captured_sleeps) == 3``).
    """
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    # Patch the ``asyncio.sleep`` reference *imported by client.py* — i.e.
    # ``whilly.adapters.transport.client.asyncio.sleep``. Patching the
    # global ``asyncio.sleep`` would leak into other modules' coroutines
    # in the same test process.
    import whilly.adapters.transport.client as client_module

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)
    yield sleeps


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token: str = "worker-token",
    bootstrap_token: str | None = None,
    backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
) -> RemoteWorkerClient:
    """Build a client wired to an :class:`httpx.MockTransport`.

    The ``handler`` callback receives every outbound :class:`httpx.Request`
    and returns the desired :class:`httpx.Response`. This is the exact
    seam the production constructor exposes via the ``transport=`` kwarg.
    """
    return RemoteWorkerClient(
        base_url="http://control-plane.example",
        token=token,
        bootstrap_token=bootstrap_token,
        backoff_schedule=backoff_schedule,
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_async_context_manager_opens_and_closes_httpx_client() -> None:
    """``__aenter__`` allocates the underlying client; ``__aexit__`` closes it.

    Using the client outside the ``async with`` block must raise rather
    than silently no-op — TASK-022b1's main loop relies on the protocol
    to scope the connection pool to the worker's lifetime.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    client = _make_client(handler)
    # Outside ``async with`` — must refuse, not silently allocate.
    with pytest.raises(RuntimeError, match="not entered"):
        await client._request("GET", "/health")

    async with client:
        response = await client._request("GET", "/health")
        assert response.status_code == 200
    # After ``__aexit__`` the same call must raise again — no silent
    # re-use of a closed pool.
    with pytest.raises(RuntimeError, match="not entered"):
        await client._request("GET", "/health")


async def test_constructor_rejects_invalid_inputs() -> None:
    """Empty base_url / token and non-positive timeout fail at construction.

    Surfacing misconfiguration here means a worker that boots with bad
    config crashes at startup rather than on its first RPC.
    """
    with pytest.raises(ValueError, match="base_url"):
        RemoteWorkerClient(base_url="", token="t")
    with pytest.raises(ValueError, match="token"):
        RemoteWorkerClient(base_url="http://x", token="")
    with pytest.raises(ValueError, match="timeout"):
        RemoteWorkerClient(base_url="http://x", token="t", timeout=0)
    with pytest.raises(ValueError, match="backoff_schedule"):
        RemoteWorkerClient(base_url="http://x", token="t", backoff_schedule=(1.0, -2.0))


# ---------------------------------------------------------------------------
# Bearer token plumbing
# ---------------------------------------------------------------------------


async def test_bearer_token_attached_to_every_request() -> None:
    """Constructor token lands as ``Authorization: Bearer <token>`` on every call."""
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})

    async with _make_client(handler, token="t-123") as client:
        await client._request("GET", "/a")
        await client._request("POST", "/b", json={"x": 1})

    assert seen_headers == ["Bearer t-123", "Bearer t-123"]


async def test_bootstrap_flag_swaps_in_bootstrap_token() -> None:
    """``bootstrap=True`` replaces the per-worker token on a single call only.

    This is the seam :func:`register` (TASK-022a2) will use: a fresh
    worker has only the cluster-wide bootstrap secret, so the register
    RPC must present it in place of the per-worker token. Subsequent
    requests revert to the regular bearer.
    """
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})

    async with _make_client(handler, token="worker-t", bootstrap_token="boot-t") as client:
        await client._request("POST", "/workers/register", bootstrap=True)
        await client._request("POST", "/tasks/claim")

    assert seen_headers == ["Bearer boot-t", "Bearer worker-t"]


async def test_bootstrap_without_token_raises() -> None:
    """``bootstrap=True`` without a configured bootstrap_token is a programmer error."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — never called
        return httpx.Response(200, json={})

    async with _make_client(handler, bootstrap_token=None) as client:
        with pytest.raises(RuntimeError, match="bootstrap_token"):
            await client._request("POST", "/workers/register", bootstrap=True)


# ---------------------------------------------------------------------------
# Retry policy — the AC's headline test
# ---------------------------------------------------------------------------


async def test_retry_on_5xx_then_succeeds(captured_sleeps: list[float]) -> None:
    """A 503 → 503 → 200 sequence retries with the documented sleep ladder.

    Three attempts total: two 5xx (each followed by a sleep) and a final
    200. The sleeps must equal the schedule's first two entries
    (``[1.0, 2.0]``) — the third entry is reserved for the *next*
    failure and stays unused on this happy-after-flakes path.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"status": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    async with _make_client(handler) as client:
        response = await client._request("GET", "/health")

    assert response.status_code == 200
    assert attempts["n"] == 3
    assert captured_sleeps == [1.0, 2.0]


async def test_retry_on_5xx_exhausts_budget_then_raises_server_error(
    captured_sleeps: list[float],
) -> None:
    """Four 5xx responses use the full ladder and surface :class:`ServerError`.

    Total attempts = 1 (initial) + len(schedule) (3 retries) = 4. The
    sleep schedule is therefore exactly the documented ``[1, 2, 4]`` —
    note that the AC's 8s value is the budget cap, *not* a fourth sleep
    (a fifth attempt would push past the long-poll budget). The exception
    preserves the final response body for the operator log.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream gone")

    async with _make_client(handler) as client:
        with pytest.raises(ServerError) as excinfo:
            await client._request("GET", "/health")

    assert captured_sleeps == [1.0, 2.0, 4.0]
    assert excinfo.value.status_code == 502
    assert excinfo.value.response_body == "upstream gone"


async def test_retry_on_connect_error_then_succeeds(captured_sleeps: list[float]) -> None:
    """:class:`httpx.ConnectError` retries identically to 5xx.

    The transport layer raises before we even see a status code; the
    retry loop must treat this exactly like a 5xx — same sleep ladder,
    same exhaustion semantics. Failing to retry connection errors would
    make the worker brittle to control-plane restarts (the TCP listener
    blip is < 100ms but covers the entire RPC otherwise).
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={})

    async with _make_client(handler) as client:
        response = await client._request("GET", "/health")

    assert response.status_code == 200
    assert attempts["n"] == 2
    assert captured_sleeps == [1.0]


async def test_retry_on_timeout_exhausts_budget(captured_sleeps: list[float]) -> None:
    """Timeouts on every attempt surface :class:`ServerError` with cause set.

    The original :class:`httpx.TimeoutException` is preserved as
    ``__cause__`` so a debugger can inspect the underlying socket state;
    callers that need it can read ``ServerError.__cause__``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    async with _make_client(handler) as client:
        with pytest.raises(ServerError) as excinfo:
            await client._request("GET", "/health")

    assert captured_sleeps == [1.0, 2.0, 4.0]
    assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)


# ---------------------------------------------------------------------------
# 4xx fail-fast
# ---------------------------------------------------------------------------


async def test_4xx_does_not_retry(captured_sleeps: list[float]) -> None:
    """A single 400 response surfaces immediately — no sleep, no retry.

    Retrying a 400 would just re-spam the same broken payload at the
    server. The fail-fast contract means the worker's supervisor sees
    the bug instantly rather than after a 7-second delay.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "bad_request", "detail": "missing field"})

    async with _make_client(handler) as client:
        with pytest.raises(HTTPClientError) as excinfo:
            await client._request("POST", "/tasks/claim", json={})

    assert attempts["n"] == 1
    assert captured_sleeps == []
    assert excinfo.value.status_code == 400
    # Plain HTTPClientError, not a more specific subclass — 400 isn't
    # auth or version-conflict, so it falls into the catch-all bucket.
    assert not isinstance(excinfo.value, (AuthError, VersionConflictError))


@pytest.mark.parametrize("status_code", [401, 403])
async def test_401_and_403_raise_auth_error(status_code: int, captured_sleeps: list[float]) -> None:
    """401 / 403 surface as :class:`AuthError`, no retries.

    Token rejection is *not* transient — retrying would spam an invalid
    bearer at the server. The worker's right move on AuthError is to
    re-register (401) or page the operator (403), not to loop.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "unauthorized"})

    async with _make_client(handler) as client:
        with pytest.raises(AuthError) as excinfo:
            await client._request("POST", "/tasks/claim", json={})

    assert captured_sleeps == []
    assert excinfo.value.status_code == status_code


async def test_409_surfaces_version_conflict_with_envelope_fields() -> None:
    """A 409 with an :class:`ErrorResponse` envelope projects all structured fields.

    The whole point of carrying ``actual_status`` / ``actual_version``
    through the exception is that TASK-022a3 / 022b1 can write
    ``except VersionConflictError as exc: if exc.actual_status ==
    TaskStatus.DONE: continue`` — i.e. treat a duplicate complete on an
    already-done task as idempotent success, no extra SELECT round-trip.
    """
    envelope = ErrorResponse(
        error="version_conflict",
        detail="version moved past expected 5; current is 7",
        task_id="TASK-022a1",
        expected_version=5,
        actual_version=7,
        actual_status=TaskStatus.DONE,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=envelope.model_dump(mode="json"))

    async with _make_client(handler) as client:
        with pytest.raises(VersionConflictError) as excinfo:
            await client._request("POST", "/tasks/TASK-022a1/complete", json={"version": 5})

    exc = excinfo.value
    assert exc.status_code == 409
    assert exc.task_id == "TASK-022a1"
    assert exc.expected_version == 5
    assert exc.actual_version == 7
    assert exc.actual_status == TaskStatus.DONE
    assert exc.error_code == "version_conflict"


async def test_409_with_malformed_envelope_still_raises_version_conflict() -> None:
    """A non-:class:`ErrorResponse` 409 body still surfaces — defensive parsing.

    A future server bug shipping a stripped-down 409 body (or a proxy
    rewriting the response) shouldn't crash the worker during exception
    construction. The structured fields are ``None`` and the error_code
    falls back to the documented constant so call sites that only check
    ``isinstance(VersionConflictError)`` keep working.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="not json at all")

    async with _make_client(handler) as client:
        with pytest.raises(VersionConflictError) as excinfo:
            await client._request("POST", "/tasks/x/complete", json={"version": 0})

    exc = excinfo.value
    assert exc.status_code == 409
    assert exc.task_id is None
    assert exc.actual_version is None
    assert exc.actual_status is None
    # Default falls back to the stable machine-readable string.
    assert exc.error_code == "version_conflict"


# ---------------------------------------------------------------------------
# Smoke test — the AC's named test_steps anchor (test_retry, test_4xx_fail_fast)
# ---------------------------------------------------------------------------


async def test_retry(captured_sleeps: list[float]) -> None:
    """AC anchor: retry on transient failure (5xx) eventually succeeds.

    Named exactly per the task's ``test_steps`` so a future audit can
    grep for ``::test_retry`` and find the canonical assertion.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"ok": True})

    async with _make_client(handler) as client:
        response = await client._request("GET", "/health")

    assert response.status_code == 200
    assert attempts["n"] == 2
    assert captured_sleeps == [1.0]


async def test_4xx_fail_fast(captured_sleeps: list[float]) -> None:
    """AC anchor: any 4xx surfaces immediately without retry.

    Named exactly per the task's ``test_steps`` so the link between the
    AC and the assertion is a single grep.
    """
    handler_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        handler_calls["n"] += 1
        return httpx.Response(404, json={"error": "not_found"})

    async with _make_client(handler) as client:
        with pytest.raises(HTTPClientError):
            await client._request("GET", "/tasks/missing")

    assert handler_calls["n"] == 1
    assert captured_sleeps == []


# ---------------------------------------------------------------------------
# 2xx body passthrough — the response is returned unparsed
# ---------------------------------------------------------------------------


async def test_2xx_response_is_returned_unparsed() -> None:
    """``_request`` is a transport primitive — it does not parse the body.

    The high-level RPC methods landing in TASK-022a2 / 022a3 pass the
    response through pydantic; this primitive returns the raw
    :class:`httpx.Response` so handler-specific schema validation lives
    in one place per RPC. Pinning the contract here means a future
    refactor cannot quietly start auto-parsing JSON and break callers
    that rely on ``response.headers`` / ``response.status_code``.
    """
    body: dict[str, Any] = {"task": {"id": "TASK-x", "status": "CLAIMED"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with _make_client(handler) as client:
        response = await client._request("POST", "/tasks/claim", json={"worker_id": "w-1"})

    assert response.status_code == 200
    assert response.json() == body
