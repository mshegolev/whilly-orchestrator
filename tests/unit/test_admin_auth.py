"""Unit tests for the M2 admin / DB-backed bootstrap auth factories.

This module covers the two new factories added in
:mod:`whilly.adapters.transport.auth` for the M2 mission:

* :func:`make_db_bootstrap_auth` — DB-backed bootstrap auth with
  optional ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` env-var fallback. Used
  by ``POST /workers/register`` after the M2 retrofit
  (VAL-M2-ADMIN-AUTH-001 / -002 / -003 / -006 / -007).
* :func:`make_admin_auth` — admin-scope auth (``is_admin=True``)
  for ``/api/v1/admin/*`` routes. 401 on missing/invalid bearer,
  403 on known non-admin operator, 200 on admin
  (VAL-M2-ADMIN-AUTH-008 / -010 / -901 / -902 / -904).

Full integration coverage (real Postgres + ASGITransport round-
trip) lives in ``tests/integration/test_admin_auth_e2e.py``;
these unit tests pin the closure contracts narrowly with a fake
repo so a regression in the dep itself surfaces without
testcontainers.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from whilly.adapters.db.repository import hash_bootstrap_token
from whilly.adapters.transport import auth as auth_module
from whilly.adapters.transport.auth import (
    SUPPRESS_WORKER_TOKEN_WARNING_ENV,
    make_admin_auth,
    make_db_bootstrap_auth,
    reset_legacy_warning_state,
)


@pytest.fixture(autouse=True)
def _reset_legacy_warning() -> Iterator[None]:
    """Clear the one-shot legacy-bootstrap-warning flag between tests."""
    reset_legacy_warning_state()
    yield
    reset_legacy_warning_state()


class _FakeRepo:
    """Minimal :class:`TaskRepository` stand-in for the bootstrap auth deps.

    Maps a plaintext token to ``(owner_email, is_admin)`` — the
    same shape the real
    :meth:`TaskRepository.get_bootstrap_token_owner` returns. The
    factories don't call any other repo method, so the fake stays
    deliberately narrow (mirrors the ``_FakeRepo`` pattern used by
    ``tests/unit/test_transport_auth.py`` for ``make_db_bearer_auth``).
    """

    def __init__(self, mapping: dict[str, tuple[str, bool]]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def get_bootstrap_token_owner(self, plaintext: str) -> tuple[str, bool] | None:
        self.calls.append(plaintext)
        if not plaintext or not plaintext.strip():
            return None
        return self._mapping.get(plaintext)


class _RaisingRepo:
    """Fake whose lookup raises — drives the 503 fallback (VAL-M2-ADMIN-AUTH-014)."""

    async def get_bootstrap_token_owner(self, plaintext: str) -> tuple[str, bool] | None:
        raise RuntimeError("simulated DB failure")


def _request_stub() -> Any:
    """Stand-in for :class:`fastapi.Request` carrying only ``state``.

    The deps only read / write ``request.state``; a
    :class:`SimpleNamespace` is functionally identical and avoids
    Starlette's scope-dict construction overhead.
    """
    return SimpleNamespace(state=SimpleNamespace())


# ---------------------------------------------------------------------------
# make_db_bootstrap_auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_bootstrap_auth_accepts_known_token_and_stashes_owner() -> None:
    """A valid per-operator bootstrap token returns success and stashes owner."""
    repo = _FakeRepo({"alice-token": ("alice@example.com", False)})
    dep = make_db_bootstrap_auth(cast(Any, repo))
    request = _request_stub()
    result = await dep(request, "Bearer alice-token")
    assert result is None
    assert request.state.bootstrap_owner_email == "alice@example.com"
    assert request.state.bootstrap_is_admin is False


@pytest.mark.asyncio
async def test_db_bootstrap_auth_rejects_unknown_token_with_401() -> None:
    """Unknown bearer → 401 + RFC 6750 WWW-Authenticate header."""
    repo = _FakeRepo({})
    dep = make_db_bootstrap_auth(cast(Any, repo))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "Bearer not-a-real-token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid bootstrap token"
    headers = exc_info.value.headers or {}
    assert headers.get("WWW-Authenticate", "").startswith("Bearer ")
    assert not hasattr(request.state, "bootstrap_owner_email")


@pytest.mark.asyncio
async def test_db_bootstrap_auth_rejects_missing_header_with_401() -> None:
    repo = _FakeRepo({"alice-token": ("alice@example.com", False)})
    dep = make_db_bootstrap_auth(cast(Any, repo))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing bearer token"


@pytest.mark.asyncio
async def test_db_bootstrap_auth_legacy_fallback_accepts_env_token() -> None:
    """Legacy env path: bearer matches ``legacy_token`` → 200 with None owner."""
    repo = _FakeRepo({})
    dep = make_db_bootstrap_auth(cast(Any, repo), legacy_token="legacy-shared")
    request = _request_stub()
    result = await dep(request, "Bearer legacy-shared")
    assert result is None
    assert request.state.bootstrap_owner_email is None
    assert request.state.bootstrap_is_admin is False


@pytest.mark.asyncio
async def test_db_bootstrap_auth_legacy_path_emits_one_shot_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per-process: exactly one deprecation WARNING per legacy hit, not per request."""
    repo = _FakeRepo({})
    dep = make_db_bootstrap_auth(cast(Any, repo), legacy_token="legacy-shared")
    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        for _ in range(3):
            request = _request_stub()
            assert await dep(request, "Bearer legacy-shared") is None
    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert len(deprecation_records) == 1, (
        f"expected exactly one legacy-bootstrap deprecation warning per process, got {len(deprecation_records)}"
    )


@pytest.mark.asyncio
async def test_db_bootstrap_auth_legacy_warning_suppressible(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`` silences the legacy warning."""
    monkeypatch.setenv(SUPPRESS_WORKER_TOKEN_WARNING_ENV, "1")
    repo = _FakeRepo({})
    dep = make_db_bootstrap_auth(cast(Any, repo), legacy_token="legacy-shared")
    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        request = _request_stub()
        await dep(request, "Bearer legacy-shared")
    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert deprecation_records == [], "deprecation WARNING must be suppressed"


@pytest.mark.asyncio
async def test_db_bootstrap_auth_per_operator_takes_precedence_over_legacy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registered per-operator token never triggers the legacy log even if the env is set."""
    repo = _FakeRepo({"alice-token": ("alice@example.com", True)})
    dep = make_db_bootstrap_auth(cast(Any, repo), legacy_token="legacy-shared")
    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        request = _request_stub()
        result = await dep(request, "Bearer alice-token")
    assert result is None
    assert request.state.bootstrap_owner_email == "alice@example.com"
    assert request.state.bootstrap_is_admin is True
    assert [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ] == []


@pytest.mark.asyncio
async def test_db_bootstrap_auth_returns_503_when_repo_raises() -> None:
    """A repo failure surfaces as 503 — never silently allow the request through."""
    dep = make_db_bootstrap_auth(cast(Any, _RaisingRepo()))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "Bearer anything")
    assert exc_info.value.status_code == 503


def test_db_bootstrap_auth_rejects_blank_legacy_token() -> None:
    """An explicit empty / whitespace ``legacy_token`` is a misconfiguration."""
    with pytest.raises(RuntimeError, match="non-empty"):
        make_db_bootstrap_auth(cast(Any, _FakeRepo({})), legacy_token="   ")


# ---------------------------------------------------------------------------
# make_admin_auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_auth_rejects_missing_bearer_with_401() -> None:
    """No DB lookup performed when the header is absent (VAL-M2-ADMIN-AUTH-010)."""
    repo = _FakeRepo({"admin-token": ("admin@example.com", True)})
    dep = make_admin_auth(cast(Any, repo))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing bearer token"
    assert repo.calls == [], "the DB must NOT be hit when the bearer header is absent"


@pytest.mark.asyncio
async def test_admin_auth_rejects_malformed_bearer_with_401() -> None:
    repo = _FakeRepo({})
    dep = make_admin_auth(cast(Any, repo))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "Basic dXNlcjpwYXNz")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid authorization scheme"
    assert repo.calls == []


@pytest.mark.asyncio
async def test_admin_auth_rejects_unknown_bearer_with_401() -> None:
    repo = _FakeRepo({"admin-token": ("admin@example.com", True)})
    dep = make_admin_auth(cast(Any, repo))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "Bearer totally-unknown")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid bootstrap token"


@pytest.mark.asyncio
async def test_admin_auth_returns_403_for_non_admin_operator() -> None:
    """Known active non-admin token → 403, not 401 (VAL-M2-ADMIN-AUTH-008)."""
    repo = _FakeRepo({"alice-token": ("alice@example.com", False)})
    dep = make_admin_auth(cast(Any, repo))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "Bearer alice-token")
    assert exc_info.value.status_code == 403
    assert "alice@example.com" in exc_info.value.detail


@pytest.mark.asyncio
async def test_admin_auth_accepts_admin_operator() -> None:
    repo = _FakeRepo({"admin-token": ("admin@example.com", True)})
    dep = make_admin_auth(cast(Any, repo))
    request = _request_stub()
    result = await dep(request, "Bearer admin-token")
    assert result is None
    assert request.state.bootstrap_owner_email == "admin@example.com"
    assert request.state.bootstrap_is_admin is True


@pytest.mark.asyncio
async def test_admin_auth_legacy_token_is_never_admin_scoped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy env-fallback token returns 403 against admin routes — never elevated."""
    repo = _FakeRepo({})
    dep = make_admin_auth(cast(Any, repo), legacy_token="legacy-shared")
    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        request = _request_stub()
        with pytest.raises(HTTPException) as exc_info:
            await dep(request, "Bearer legacy-shared")
    assert exc_info.value.status_code == 403
    # Deprecation warning still fires so the operator notices.
    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert len(deprecation_records) == 1


@pytest.mark.asyncio
async def test_admin_auth_returns_503_when_repo_raises() -> None:
    dep = make_admin_auth(cast(Any, _RaisingRepo()))
    request = _request_stub()
    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "Bearer anything")
    assert exc_info.value.status_code == 503


def test_admin_auth_rejects_blank_legacy_token() -> None:
    with pytest.raises(RuntimeError, match="non-empty"):
        make_admin_auth(cast(Any, _FakeRepo({})), legacy_token=" ")


# ---------------------------------------------------------------------------
# Hashing helpers — module-level wiring
# ---------------------------------------------------------------------------


def test_hash_bootstrap_token_is_sha256_hex() -> None:
    """The repo and the auth deps share one hashing primitive."""
    digest = hash_bootstrap_token("alice-token")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_make_db_bootstrap_auth_and_admin_auth_are_distinct_callables() -> None:
    """A bootstrap dep must not also accept admin requests, and vice versa."""
    repo = _FakeRepo({})
    boot_dep = make_db_bootstrap_auth(cast(Any, repo))
    admin_dep = make_admin_auth(cast(Any, repo))
    assert boot_dep is not admin_dep
    assert callable(boot_dep)
    assert callable(admin_dep)


def test_module_level_factories_exported() -> None:
    """Public factories appear in ``__all__`` so static-analysis tools pick them up."""
    assert "make_admin_auth" in auth_module.__all__
    assert "make_db_bootstrap_auth" in auth_module.__all__
