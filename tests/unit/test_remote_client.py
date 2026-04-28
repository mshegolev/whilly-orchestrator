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
    REGISTER_PATH,
    AuthError,
    HTTPClientError,
    RemoteWorkerClient,
    ServerError,
    VersionConflictError,
    heartbeat_path,
)
from whilly.adapters.transport.schemas import (
    ErrorResponse,
    HeartbeatResponse,
    RegisterResponse,
)
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


# ---------------------------------------------------------------------------
# register / heartbeat — TASK-022a2
# ---------------------------------------------------------------------------


async def test_register_uses_bootstrap_token_and_register_path() -> None:
    """``register(hostname)`` POSTs to /workers/register with the bootstrap header.

    The AC's headline contract is the *split* between bootstrap and
    per-worker tokens: the registration RPC must carry the bootstrap
    secret, not the per-worker bearer. A regression here would break
    the PRD FR-1.2 token-rotation story (bootstrap rotation must not
    invalidate per-worker bearers, and vice-versa).

    The test records the wire request — method, path, body, and the
    Authorization header — and asserts each is what the AC pins.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.read().decode()
        return httpx.Response(
            201,
            json={"worker_id": "w-abc", "token": "fresh-per-worker-token"},
        )

    async with _make_client(
        handler,
        token="placeholder-pre-register",
        bootstrap_token="boot-secret",
    ) as client:
        response = await client.register(hostname="host-alpha")

    assert isinstance(response, RegisterResponse)
    assert response.worker_id == "w-abc"
    assert response.token == "fresh-per-worker-token"

    assert captured["method"] == "POST"
    assert captured["path"] == REGISTER_PATH
    # The bootstrap header MUST be in place — not the per-worker token.
    assert captured["auth"] == "Bearer boot-secret"
    # Body round-trips the pydantic schema.
    import json

    assert json.loads(captured["body"]) == {"hostname": "host-alpha"}


async def test_register_does_not_mutate_per_worker_token() -> None:
    """``register`` is a transport primitive — it does not swap the bearer.

    Pinned because the design rationale (in :meth:`register`'s docstring)
    explicitly rejects token-mutation as a side effect: the caller
    decides what to do with the freshly-issued token. A regression
    that started overwriting ``self._token`` would silently change
    the semantics for every subsequent RPC on the same client.

    The handler branches on the URL path: ``register`` should arrive
    with the bootstrap bearer, and an immediately-following
    ``heartbeat`` on the same client must still see the *original*
    per-worker bearer — proving no swap happened.
    """
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen[path] = request.headers.get("Authorization")
        if path == REGISTER_PATH:
            return httpx.Response(201, json={"worker_id": "w-x", "token": "new-token"})
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected path {path}")

    async with _make_client(
        handler,
        token="original-worker-token",
        bootstrap_token="boot",
    ) as client:
        await client.register(hostname="host-beta")
        await client.heartbeat(worker_id="w-x")

    assert seen[REGISTER_PATH] == "Bearer boot"
    assert seen[heartbeat_path("w-x")] == "Bearer original-worker-token"


async def test_register_without_bootstrap_token_raises() -> None:
    """A client constructed without ``bootstrap_token`` cannot call ``register``.

    The error surfaces the missing constructor kwarg explicitly so an
    operator can fix the supervisor-side wiring instead of chasing a
    cryptic 401 from the server.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — never called
        return httpx.Response(201, json={"worker_id": "w", "token": "t"})

    async with _make_client(handler, bootstrap_token=None) as client:
        with pytest.raises(RuntimeError, match="bootstrap_token"):
            await client.register(hostname="host")


async def test_register_propagates_auth_error_on_401() -> None:
    """A wrong / rotated bootstrap token surfaces as :class:`AuthError`, not silent retry.

    The retry policy must NOT cover 4xx — a flapping register loop
    against a server with a rotated bootstrap secret would burn cycles
    forever. This test pins that contract end-to-end through
    ``register``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _make_client(handler, bootstrap_token="wrong") as client:
        with pytest.raises(AuthError) as excinfo:
            await client.register(hostname="host")

    assert excinfo.value.status_code == 401


async def test_register_rejects_empty_hostname_at_schema_layer() -> None:
    """An empty hostname raises :class:`pydantic.ValidationError` *before* the network call.

    The schema's ``min_length=1`` validation catches programmer errors
    at the right tier — surfacing as a wire-level 422 would force the
    operator to read a server log instead of a stack trace from the
    worker process itself.
    """
    handler_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — never called
        handler_calls["n"] += 1
        return httpx.Response(201, json={"worker_id": "w", "token": "t"})

    from pydantic import ValidationError

    async with _make_client(handler, bootstrap_token="boot") as client:
        with pytest.raises(ValidationError):
            await client.register(hostname="")

    # The network was never touched — the schema caught it at the boundary.
    assert handler_calls["n"] == 0


async def test_register_surfaces_server_error_on_malformed_response() -> None:
    """A 2xx response that doesn't match :class:`RegisterResponse` becomes :class:`ServerError`.

    The schema-mismatch case is *not* retryable: the server already
    succeeded HTTP-wise, the body is just wrong. Surfacing as
    :class:`ServerError` keeps the worker's outer-loop classifier
    simple — the same exception class covers "5xx exhausted retries"
    and "server returned a body we can't validate".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Missing the required ``token`` field — extra=forbid + min_length checks
        # in :class:`RegisterResponse` mean validation fails.
        return httpx.Response(201, json={"worker_id": "w-only"})

    async with _make_client(handler, bootstrap_token="boot") as client:
        with pytest.raises(ServerError) as excinfo:
            await client.register(hostname="host")

    # The cause chain preserves the underlying ValidationError so a
    # debugger can inspect which field failed.
    from pydantic import ValidationError as PydanticValidationError

    assert isinstance(excinfo.value.__cause__, PydanticValidationError)


async def test_register_surfaces_server_error_on_non_json_body() -> None:
    """A 2xx HTML/text body becomes :class:`ServerError` rather than crashing inside json().

    A misbehaving proxy (e.g. an HTML 200 page from a captive portal)
    would otherwise raise an opaque ``ValueError`` deep in httpx; the
    typed surface here means the supervisor logs see a familiar shape.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="<html>not json</html>")

    async with _make_client(handler, bootstrap_token="boot") as client:
        with pytest.raises(ServerError) as excinfo:
            await client.register(hostname="host")

    assert "non-JSON" in str(excinfo.value)


async def test_register_retries_on_transient_5xx(captured_sleeps: list[float]) -> None:
    """``register`` inherits :meth:`_request`'s retry ladder for transient 5xx.

    The PRD allows registration to flake during a deploy; what it
    forbids is silent retries on 4xx. A 503 → 503 → 201 sequence here
    must succeed with the documented sleep ladder.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"status": "unavailable"})
        return httpx.Response(201, json={"worker_id": "w", "token": "t"})

    async with _make_client(handler, bootstrap_token="boot") as client:
        response = await client.register(hostname="host")

    assert response.worker_id == "w"
    assert attempts["n"] == 3
    assert captured_sleeps == [1.0, 2.0]


async def test_heartbeat_uses_bearer_and_dynamic_path() -> None:
    """``heartbeat(worker_id)`` POSTs to /workers/{id}/heartbeat with the per-worker bearer.

    The two load-bearing facts: (1) the dynamic path includes the
    worker_id from the argument, (2) the per-worker bearer (NOT the
    bootstrap secret) authenticates the call.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    async with _make_client(
        handler,
        token="bearer-tok",
        bootstrap_token="boot-tok",
    ) as client:
        response = await client.heartbeat(worker_id="w-42")

    assert isinstance(response, HeartbeatResponse)
    assert response.ok is True

    assert captured["method"] == "POST"
    assert captured["path"] == "/workers/w-42/heartbeat"
    assert captured["path"] == heartbeat_path("w-42")
    # Per-worker bearer, NOT the bootstrap token.
    assert captured["auth"] == "Bearer bearer-tok"
    import json

    assert json.loads(captured["body"]) == {"worker_id": "w-42"}


async def test_heartbeat_returns_ok_false_for_unknown_worker() -> None:
    """``ok=False`` is a *recoverable* state — the method returns it, not raises.

    The whole point of the AC's dichotomy "raises on 4xx, returns on
    2xx" is that a worker whose row was admin-revoked sees
    ``ok=False`` and re-registers in TASK-022b2's heartbeat loop. A
    regression that started raising here would crash that loop and
    make the worker unrecoverable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    async with _make_client(handler) as client:
        response = await client.heartbeat(worker_id="w-revoked")

    assert response.ok is False


async def test_heartbeat_propagates_auth_error_on_401() -> None:
    """A rotated per-worker bearer surfaces as :class:`AuthError` from heartbeat.

    Mirrors :func:`test_register_propagates_auth_error_on_401` for the
    per-worker bearer path. The supervisor's response is to re-register
    (TASK-022b2) — but that's the supervisor's job, this RPC just
    surfaces the typed signal.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _make_client(handler, token="rotated-out") as client:
        with pytest.raises(AuthError) as excinfo:
            await client.heartbeat(worker_id="w-1")

    assert excinfo.value.status_code == 401


async def test_heartbeat_rejects_empty_worker_id_at_schema_layer() -> None:
    """``heartbeat("")`` raises :class:`pydantic.ValidationError` before any wire call."""
    handler_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — never called
        handler_calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    from pydantic import ValidationError

    async with _make_client(handler) as client:
        with pytest.raises(ValidationError):
            await client.heartbeat(worker_id="")

    assert handler_calls["n"] == 0


async def test_heartbeat_surfaces_server_error_on_schema_drift() -> None:
    """A 200 with a body that doesn't match :class:`HeartbeatResponse` becomes :class:`ServerError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Bogus field, no ``ok`` — extra=forbid + missing required.
        return httpx.Response(200, json={"unknown_key": "value"})

    async with _make_client(handler) as client:
        with pytest.raises(ServerError):
            await client.heartbeat(worker_id="w-1")


async def test_heartbeat_retries_on_transient_5xx(captured_sleeps: list[float]) -> None:
    """``heartbeat`` inherits the retry ladder from :meth:`_request`.

    Heartbeat blips during a control-plane deploy must not bring down
    the worker — the supervisor's outer loop in TASK-022b2 already
    catches exceptions, but transient 5xx should clear at the RPC tier.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"ok": True})

    async with _make_client(handler) as client:
        response = await client.heartbeat(worker_id="w-1")

    assert response.ok is True
    assert attempts["n"] == 2
    assert captured_sleeps == [1.0]


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
