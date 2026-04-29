"""Unit tests for the per-worker bearer dependency (TASK-101 / VAL-AUTH-*).

This module exercises :func:`whilly.adapters.transport.auth.make_db_bearer_auth`
in isolation — no Postgres, no FastAPI app instance, no httpx. The
dep accepts a :class:`whilly.adapters.db.TaskRepository` (or any
structurally compatible object exposing
:meth:`get_worker_id_by_token_hash`); we hand it a
:class:`_FakeTaskRepository` whose lookup table is a plain dict so
each test can inspect / mutate the registered hashes without
threading testcontainers through the unit suite. Integration coverage
of the full Postgres path lives in
``tests/integration/test_per_worker_auth.py``.

What we cover here
------------------
* :func:`make_db_bearer_auth` accepts a hit on the repo lookup (a
  registered worker's plaintext bearer hashes to a row) and the
  201-shape "no return / no raise" success path.
* Mismatched / unknown bearers raise the canonical 401 with the
  RFC 6750 ``WWW-Authenticate: Bearer realm="whilly"`` header
  (VAL-AUTH-020).
* Missing ``Authorization`` header / non-Bearer scheme / empty
  token after the prefix all surface as 401 *before* the repo
  lookup runs (VAL-AUTH-021 / VAL-AUTH-022) — assertion: the fake
  repo's hit counter stays at zero.
* The legacy ``WHILLY_WORKER_TOKEN`` fallback is gated by the
  ``legacy_token`` factory kwarg:
  - ``None`` (the v4.2 future shape) — every non-DB-hit request
    returns 401 even if the bearer happens to equal a string the
    operator might have set (VAL-AUTH-033).
  - non-empty — a request whose bearer matches the legacy string
    returns success AND emits the one-shot deprecation warning
    via ``log.warning`` (VAL-AUTH-030 / VAL-AUTH-031).
* The deprecation warning is one-shot: emitted on the first match,
  silenced on every subsequent match within the same process
  (VAL-AUTH-031). :func:`reset_legacy_warning_state` resets the
  flag for tests that need to exercise the emission again.
* ``WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`` silences the warning
  even on the very first match, while still letting the request
  through (VAL-AUTH-032).
* Per-worker bearer takes precedence: when the bearer is *both* a
  registered hash and equal to the legacy token, the dep accepts
  on the per-worker path and does NOT log the deprecation
  (VAL-AUTH-034).
* Empty / whitespace-only ``legacy_token`` kwargs are rejected at
  factory construction time — same defence-in-depth as
  :func:`make_bearer_auth`'s empty-token guard.

Why fakes, not mocks
--------------------
A hand-rolled :class:`_FakeTaskRepository` (~15 lines) reads as a
contract at the call site: ``register("hash-A", "worker-A")`` is
self-explanatory, while ``mock.return_value = "worker-A"`` would
hide the lookup-by-hash semantics behind a setup ritual. The fake
also tracks how many times the lookup ran so the "missing header
short-circuits before the DB" assertion is straightforward.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from whilly.adapters.transport import auth as auth_module
from whilly.adapters.transport.auth import (
    SUPPRESS_WORKER_TOKEN_WARNING_ENV,
    hash_bearer_token,
    make_db_bearer_auth,
    reset_legacy_warning_state,
)


def _request_stub() -> Any:
    """Build a stand-in for :class:`fastapi.Request`.

    :func:`make_db_bearer_auth` reads / writes only ``request.state``
    (Starlette's free-form attribute bag), so a :class:`SimpleNamespace`
    carrying its own nested ``state`` namespace is functionally
    identical to the real Starlette ``State`` object for our purposes.
    Avoids constructing a full Starlette ``Request`` (which needs an
    ASGI ``scope`` dict that would add no signal here).
    """
    return SimpleNamespace(state=SimpleNamespace())


class _FakeTaskRepository:
    """Minimal stand-in for :class:`whilly.adapters.db.TaskRepository`.

    Implements only the surface :func:`make_db_bearer_auth` uses —
    :meth:`get_worker_id_by_token_hash`. ``hits`` counts the number
    of lookups so tests can assert on call patterns (e.g. "an empty
    Authorization header must short-circuit before the repo runs").
    """

    def __init__(self) -> None:
        self._table: dict[str, str] = {}
        self.hits: int = 0

    def register(self, plaintext: str, worker_id: str) -> None:
        """Helper: hash ``plaintext`` and remember it maps to ``worker_id``.

        Mirrors what :meth:`TaskRepository.register_worker` writes to
        ``workers.token_hash`` so tests don't have to know the hashing
        scheme to set up a fixture.
        """
        self._table[hash_bearer_token(plaintext)] = worker_id

    async def get_worker_id_by_token_hash(self, token_hash: str) -> str | None:
        """Look up ``token_hash`` in the in-memory table; bump :attr:`hits`."""
        self.hits += 1
        return self._table.get(token_hash)


@pytest.fixture(autouse=True)
def _reset_legacy_warning_state() -> Iterator[None]:
    """Clear the one-shot legacy-bearer warning flag between tests.

    The module-level guard in
    :mod:`whilly.adapters.transport.auth` persists across tests by
    design (so the deprecation warning surfaces at most once per
    process). For unit tests we want a clean slate per case.
    """
    reset_legacy_warning_state()
    yield
    reset_legacy_warning_state()


# ---------------------------------------------------------------------------
# Happy path — registered worker bearer authenticates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_bearer_auth_accepts_registered_token() -> None:
    """A bearer whose hash matches a workers row → success (no raise)."""
    repo = _FakeTaskRepository()
    repo.register("plaintext-A", "w-alpha")

    dep = make_db_bearer_auth(repo)
    request = _request_stub()
    result = await dep(request, "Bearer plaintext-A")

    assert result is None
    assert repo.hits == 1, "DB lookup should have run exactly once"
    # Identity-binding contract (TASK-101 scrutiny round-1 fix):
    # successful per-worker hash hit stashes worker_id on request.state
    # so the route handler's ``_require_token_owner`` check can compare
    # against the body / path identity (see VAL-AUTH-024).
    assert request.state.authenticated_worker_id == "w-alpha"


# ---------------------------------------------------------------------------
# 401 paths — pre-DB short-circuits and DB miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_bearer_auth_rejects_unknown_token() -> None:
    """A bearer that doesn't hash to any registered row → 401 invalid token."""
    repo = _FakeTaskRepository()
    repo.register("plaintext-A", "w-alpha")

    dep = make_db_bearer_auth(repo)
    with pytest.raises(HTTPException) as exc_info:
        await dep(_request_stub(), "Bearer some-other-bearer")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid token"
    headers = exc_info.value.headers or {}
    assert headers.get("WWW-Authenticate", "").startswith("Bearer ")
    assert 'realm="whilly"' in headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_db_bearer_auth_rejects_missing_header_without_db_lookup() -> None:
    """Missing Authorization header → 401 BEFORE the DB lookup runs."""
    repo = _FakeTaskRepository()

    dep = make_db_bearer_auth(repo)
    with pytest.raises(HTTPException) as exc_info:
        await dep(_request_stub(), None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing bearer token"
    assert repo.hits == 0, "DB must not be queried when the bearer is missing"


@pytest.mark.asyncio
async def test_db_bearer_auth_rejects_non_bearer_scheme_without_db_lookup() -> None:
    """A ``Basic ...`` (or other non-Bearer) scheme → 401 invalid scheme.

    Pinned because :func:`_extract_bearer` must short-circuit before
    the DB lookup — otherwise a misbehaving client doing ``Basic
    user:pass`` would force a DB round-trip per request, amplifying
    a trivial DoS surface.
    """
    repo = _FakeTaskRepository()

    dep = make_db_bearer_auth(repo)
    with pytest.raises(HTTPException) as exc_info:
        await dep(_request_stub(), "Basic dXNlcjpwYXNz")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid authorization scheme"
    assert repo.hits == 0


@pytest.mark.asyncio
async def test_db_bearer_auth_rejects_empty_token_after_prefix() -> None:
    """``Authorization: Bearer    `` (whitespace only) → 401 before DB."""
    repo = _FakeTaskRepository()

    dep = make_db_bearer_auth(repo)
    with pytest.raises(HTTPException) as exc_info:
        await dep(_request_stub(), "Bearer    ")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "empty bearer token"
    assert repo.hits == 0


# ---------------------------------------------------------------------------
# Legacy fallback — WHILLY_WORKER_TOKEN deprecation path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_bearer_auth_legacy_match_emits_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request matching ``legacy_token`` succeeds AND logs a WARNING (VAL-AUTH-030)."""
    repo = _FakeTaskRepository()
    dep = make_db_bearer_auth(repo, legacy_token="shared-xyz")

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        request = _request_stub()
        result = await dep(request, "Bearer shared-xyz")

    assert result is None
    # Legacy fallback explicitly stamps identity as None — the shared
    # cluster bearer cannot identify a specific worker.
    assert request.state.authenticated_worker_id is None
    deprecation_records = [rec for rec in caplog.records if "deprecated" in rec.getMessage().lower()]
    assert len(deprecation_records) == 1, "exactly one WARNING should mention 'deprecated'"
    assert deprecation_records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_db_bearer_auth_legacy_warning_is_one_shot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive legacy hits → exactly ONE WARNING (VAL-AUTH-031)."""
    repo = _FakeTaskRepository()
    dep = make_db_bearer_auth(repo, legacy_token="shared-xyz")

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        await dep(_request_stub(), "Bearer shared-xyz")
        await dep(_request_stub(), "Bearer shared-xyz")

    deprecation_records = [rec for rec in caplog.records if "deprecated" in rec.getMessage().lower()]
    assert len(deprecation_records) == 1, "the deprecation warning must be emitted at most once per process"


@pytest.mark.asyncio
async def test_db_bearer_auth_legacy_warning_suppressed_by_env(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`` silences the warning (VAL-AUTH-032).

    The fallback itself remains active — only the journal noise is
    suppressed. The request still succeeds; ``caplog`` simply
    contains no deprecation record.
    """
    monkeypatch.setenv(SUPPRESS_WORKER_TOKEN_WARNING_ENV, "1")
    repo = _FakeTaskRepository()
    dep = make_db_bearer_auth(repo, legacy_token="shared-xyz")

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        result = await dep(_request_stub(), "Bearer shared-xyz")

    assert result is None
    deprecation_records = [rec for rec in caplog.records if "deprecated" in rec.getMessage().lower()]
    assert deprecation_records == [], "WARNING must be suppressed by the env opt-out"


@pytest.mark.asyncio
async def test_db_bearer_auth_legacy_disabled_when_token_is_none() -> None:
    """``legacy_token=None`` → no fallback; any non-DB-hit returns 401 (VAL-AUTH-033)."""
    repo = _FakeTaskRepository()
    dep = make_db_bearer_auth(repo, legacy_token=None)

    with pytest.raises(HTTPException) as exc_info:
        await dep(_request_stub(), "Bearer would-have-matched-shared-token")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid token"


@pytest.mark.asyncio
async def test_db_bearer_auth_per_worker_takes_precedence_over_legacy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registered bearer matching the legacy token still authenticates as per-worker (VAL-AUTH-034).

    Pinned because per-worker precedence is the entire point of TASK-101:
    the deprecation warning must NOT fire for a request that authenticates
    against the per-worker hash, even if the same bearer string would
    also have matched ``legacy_token``.
    """
    repo = _FakeTaskRepository()
    repo.register("plaintext-A", "w-alpha")
    # Legacy token equals the per-worker plaintext (vanishingly unlikely
    # in production but exercises the precedence rule).
    dep = make_db_bearer_auth(repo, legacy_token="plaintext-A")

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        request = _request_stub()
        result = await dep(request, "Bearer plaintext-A")

    assert result is None
    # Per-worker precedence: identity stamped to the resolved worker_id,
    # NOT to None (which would have indicated a legacy fallback hit).
    assert request.state.authenticated_worker_id == "w-alpha"
    deprecation_records = [rec for rec in caplog.records if "deprecated" in rec.getMessage().lower()]
    assert deprecation_records == [], "per-worker authentication must NOT trigger the legacy deprecation log"


@pytest.mark.asyncio
async def test_db_bearer_auth_legacy_token_constant_time_compare() -> None:
    """The legacy fallback uses :func:`secrets.compare_digest` semantics.

    A near-miss (wrong by one character) is still rejected. We cannot
    directly inspect timing here, but we can pin the *outcome*: the
    fallback rejects bearers that differ from ``legacy_token`` even
    by a single byte.
    """
    repo = _FakeTaskRepository()
    dep = make_db_bearer_auth(repo, legacy_token="shared-xyz")

    with pytest.raises(HTTPException) as exc_info:
        await dep(_request_stub(), "Bearer shared-xy")  # one char short

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid token"


# ---------------------------------------------------------------------------
# Factory guard — empty/whitespace ``legacy_token`` kwarg
# ---------------------------------------------------------------------------


def test_make_db_bearer_auth_rejects_blank_legacy_token() -> None:
    """An empty / whitespace-only ``legacy_token`` is a misconfiguration.

    Operators who genuinely want to disable the fallback pass ``None``
    (or omit the kwarg). Accepting an empty string would create a
    silent foot-gun where ``compare_digest("", "")`` short-circuits
    True for every empty bearer.
    """
    repo = _FakeTaskRepository()
    with pytest.raises(RuntimeError, match="non-empty"):
        make_db_bearer_auth(repo, legacy_token="   ")


# ---------------------------------------------------------------------------
# hash_bearer_token — ensure exposed helper matches register-side hashing
# ---------------------------------------------------------------------------


def test_hash_bearer_token_is_sha256_hex() -> None:
    """The exposed helper hashes via SHA-256 (PRD NFR-3, VAL-AUTH-011 alignment).

    Keeping this test in the unit suite means a future migration of
    the hashing scheme (argon2 / scrypt) will fire here AND in the
    integration tests that pin :class:`TaskRepository.register_worker`'s
    write side — both must update lock-step.
    """
    import hashlib as _hashlib

    plaintext = "abc-123"
    expected = _hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert hash_bearer_token(plaintext) == expected


# ---------------------------------------------------------------------------
# reset_legacy_warning_state — public test seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_legacy_warning_state_re_arms_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests can re-arm the one-shot warning across cases via the public reset.

    The autouse fixture in this module already calls
    :func:`reset_legacy_warning_state`, but a feature consumer
    (mission-level integration tests) may need the same reset
    inside a single test that spans two ``create_app`` instances.
    Pinning the API here documents the seam.
    """
    repo = _FakeTaskRepository()
    dep = make_db_bearer_auth(repo, legacy_token="shared-xyz")

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        await dep(_request_stub(), "Bearer shared-xyz")
        await dep(_request_stub(), "Bearer shared-xyz")  # second hit — flag set, no log

        reset_legacy_warning_state()
        await dep(_request_stub(), "Bearer shared-xyz")  # third hit — flag cleared, log fires

    deprecation_records = [rec for rec in caplog.records if "deprecated" in rec.getMessage().lower()]
    assert len(deprecation_records) == 2, "reset_legacy_warning_state should re-arm the one-shot guard"


def test_module_level_legacy_warning_flag_is_module_global() -> None:
    """Defence-in-depth: the guard is a module-level boolean (mirrors dotenv)."""
    assert hasattr(auth_module, "_legacy_worker_token_warning_emitted")
    assert isinstance(auth_module._legacy_worker_token_warning_emitted, bool)
