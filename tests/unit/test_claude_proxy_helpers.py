"""Unit tests for proxy helpers (TASK-109-1).

Covers :class:`ProxySettings`, :func:`resolve_proxy_settings`, and
:func:`build_subprocess_env` — pure functions, no I/O. The probe
function is exercised separately in ``test_claude_proxy_probe.py``
because it needs a fake TCP server fixture.

PRD: ``docs/PRD-v41-claude-proxy.md`` FR-1 / FR-5 / FR-6 / OQ-2 / OQ-4.
"""

from __future__ import annotations

import pytest

from whilly.adapters.runner.proxy import (
    DEFAULT_NO_PROXY,
    INHERITED_HTTPS_PROXY_ENV,
    ProxySettings,
    WHILLY_NO_PROXY_ENV,
    WHILLY_PROXY_URL_ENV,
    build_subprocess_env,
    resolve_proxy_settings,
)


# ─── ProxySettings.is_active ──────────────────────────────────────────────


def test_is_active_url_none() -> None:
    assert ProxySettings(url=None).is_active is False


def test_is_active_url_empty_string() -> None:
    assert ProxySettings(url="").is_active is False


def test_is_active_url_set() -> None:
    assert ProxySettings(url="http://127.0.0.1:11112").is_active is True


# ─── resolve_proxy_settings: priority chain (FR-1, FR-6, OQ-2) ────────────


def test_resolve_cli_disabled_overrides_everything() -> None:
    """`--no-claude-proxy` opts out even when both env vars are set."""
    env = {
        WHILLY_PROXY_URL_ENV: "http://from-whilly-env:1",
        INHERITED_HTTPS_PROXY_ENV: "http://from-inherited:2",
    }
    settings = resolve_proxy_settings(
        cli_url="http://from-cli:3",
        cli_disabled=True,
        env=env,
    )
    assert settings.url is None
    assert settings.is_active is False


def test_resolve_cli_url_wins_over_env() -> None:
    """`--claude-proxy URL` overrides both WHILLY_CLAUDE_PROXY_URL and HTTPS_PROXY."""
    env = {
        WHILLY_PROXY_URL_ENV: "http://from-whilly-env:1",
        INHERITED_HTTPS_PROXY_ENV: "http://from-inherited:2",
    }
    settings = resolve_proxy_settings(cli_url="http://from-cli:3", env=env)
    assert settings.url == "http://from-cli:3"


def test_resolve_whilly_env_wins_over_inherited() -> None:
    """WHILLY_CLAUDE_PROXY_URL wins over inherited HTTPS_PROXY when no CLI flag."""
    env = {
        WHILLY_PROXY_URL_ENV: "http://from-whilly:1",
        INHERITED_HTTPS_PROXY_ENV: "http://from-inherited:2",
    }
    settings = resolve_proxy_settings(env=env)
    assert settings.url == "http://from-whilly:1"


def test_resolve_falls_back_to_inherited_https_proxy() -> None:
    """No CLI flag, no WHILLY_CLAUDE_PROXY_URL → use HTTPS_PROXY (claudeproxy shell flow)."""
    env = {INHERITED_HTTPS_PROXY_ENV: "http://from-inherited:2"}
    settings = resolve_proxy_settings(env=env)
    assert settings.url == "http://from-inherited:2"


def test_resolve_no_signal_returns_none() -> None:
    """No CLI flag, no env vars → None (default off)."""
    settings = resolve_proxy_settings(env={})
    assert settings.url is None
    assert settings.is_active is False


def test_resolve_strips_whitespace_in_env() -> None:
    """Env values with leading/trailing whitespace should be treated as empty."""
    env = {WHILLY_PROXY_URL_ENV: "   "}
    settings = resolve_proxy_settings(env=env)
    assert settings.url is None


def test_resolve_empty_string_cli_url_treated_as_no_flag() -> None:
    """``cli_url=''`` is the falsy default (argparse default) — falls through."""
    env = {WHILLY_PROXY_URL_ENV: "http://from-env:1"}
    settings = resolve_proxy_settings(cli_url="", env=env)
    assert settings.url == "http://from-env:1"


# ─── no_proxy resolution (OQ-4) ───────────────────────────────────────────


def test_no_proxy_default_when_env_missing() -> None:
    """Without WHILLY_CLAUDE_NO_PROXY → uses default `localhost,127.0.0.1,::1`."""
    settings = resolve_proxy_settings(cli_url="http://x:1", env={})
    assert settings.no_proxy == DEFAULT_NO_PROXY


def test_no_proxy_env_override_wins() -> None:
    env = {WHILLY_NO_PROXY_ENV: "*.internal,10.0.0.0/8"}
    settings = resolve_proxy_settings(cli_url="http://x:1", env=env)
    assert settings.no_proxy == "*.internal,10.0.0.0/8"


def test_no_proxy_explicit_empty_string_honoured() -> None:
    """Operator can opt out of the default exclusions with empty string."""
    env = {WHILLY_NO_PROXY_ENV: ""}
    settings = resolve_proxy_settings(cli_url="http://x:1", env=env)
    assert settings.no_proxy == ""


def test_no_proxy_default_argument_overrides_global_default() -> None:
    """Caller can pass a different default if their app has different defaults."""
    settings = resolve_proxy_settings(env={}, default_no_proxy="custom-default")
    assert settings.no_proxy == "custom-default"


# ─── build_subprocess_env (FR-2, FR-5) ────────────────────────────────────


def test_build_env_inactive_returns_unchanged_copy() -> None:
    """When proxy is off, the function is a no-op except for the dict copy."""
    parent = {"PATH": "/usr/bin", "HOME": "/home/user"}
    settings = ProxySettings(url=None)

    result = build_subprocess_env(parent, settings)

    assert result == parent
    assert result is not parent  # always a fresh dict
    assert "HTTPS_PROXY" not in result
    assert "NO_PROXY" not in result


def test_build_env_active_injects_https_proxy_and_no_proxy() -> None:
    """When proxy is on, both HTTPS_PROXY and NO_PROXY are set on the diff."""
    parent = {"PATH": "/usr/bin"}
    settings = ProxySettings(url="http://127.0.0.1:11112", no_proxy="localhost,::1")

    result = build_subprocess_env(parent, settings)

    assert result["PATH"] == "/usr/bin"
    assert result["HTTPS_PROXY"] == "http://127.0.0.1:11112"
    assert result["NO_PROXY"] == "localhost,::1"


def test_build_env_overrides_existing_https_proxy() -> None:
    """Settings always win over whatever the parent env had inherited."""
    parent = {"HTTPS_PROXY": "http://stale:1", "NO_PROXY": "stale"}
    settings = ProxySettings(url="http://fresh:2", no_proxy="fresh-no-proxy")

    result = build_subprocess_env(parent, settings)

    assert result["HTTPS_PROXY"] == "http://fresh:2"
    assert result["NO_PROXY"] == "fresh-no-proxy"


def test_build_env_does_not_mutate_parent() -> None:
    """The same parent dict can be passed multiple times safely (worker loop reuse)."""
    parent = {"PATH": "/usr/bin"}
    settings = ProxySettings(url="http://x:1")

    build_subprocess_env(parent, settings)
    build_subprocess_env(parent, settings)

    # parent unchanged after two calls.
    assert parent == {"PATH": "/usr/bin"}


# ─── ProxySettings is frozen (defensive — catches accidental mutation) ────


def test_proxy_settings_is_frozen() -> None:
    settings = ProxySettings(url="http://x:1")
    with pytest.raises(Exception):  # FrozenInstanceError on dataclass(frozen=True)
        settings.url = "http://other:2"  # type: ignore[misc]
