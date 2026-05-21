"""Unit tests for the auth URL-safety helpers (post-E15 review, Findings 5 + 7).

Pure functions from ``whilly.api.auth_routes``:

* ``_build_magic_url`` / ``_public_base_url`` — a magic link must be built from
  the configured ``WHILLY_PUBLIC_ORIGIN``, not the client-controlled ``Host``
  header (host-header injection → token harvest → account takeover, Finding 5).
* ``_sanitise_next_path`` — ``?next=`` must stay a local path; the backslash
  variants browsers fold to ``//`` must be rejected (open redirect, Finding 7).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from whilly.api.auth_routes import (
    PUBLIC_ORIGIN_ENV,
    _build_magic_url,
    _public_base_url,
    _sanitise_next_path,
)


def _req(base_url: str) -> object:
    return SimpleNamespace(base_url=base_url)


def test_magic_url_uses_configured_origin_not_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, "https://whilly.example.com")
    # An attacker-controlled Host must be ignored when the origin is pinned.
    url = _build_magic_url(_req("https://attacker.test/"), "tok123")
    assert url.startswith("https://whilly.example.com/auth/magic?token=")
    assert "attacker.test" not in url


def test_magic_url_falls_back_to_host_when_origin_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    url = _build_magic_url(_req("http://127.0.0.1:8000/"), "tok123")
    assert url == "http://127.0.0.1:8000/auth/magic?token=tok123"


def test_public_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, "https://whilly.example.com/")
    assert _public_base_url(_req("http://unused/")) == "https://whilly.example.com"


@pytest.mark.parametrize("evil", ["//evil.com", "/\\evil.com", "\\/evil.com", "\\\\evil.com", "/path\\to"])
def test_sanitise_next_rejects_redirect_tricks(evil: str) -> None:
    assert _sanitise_next_path(evil) == "/"


@pytest.mark.parametrize("absolute", ["http://evil.com", "https://evil.com", "javascript:alert(1)"])
def test_sanitise_next_rejects_absolute_urls(absolute: str) -> None:
    assert _sanitise_next_path(absolute) == "/"


def test_sanitise_next_allows_local_deep_link() -> None:
    assert _sanitise_next_path("/plans/foo?tab=tasks#x") == "/plans/foo?tab=tasks#x"


def test_sanitise_next_defaults_root_on_empty() -> None:
    assert _sanitise_next_path("") == "/"
