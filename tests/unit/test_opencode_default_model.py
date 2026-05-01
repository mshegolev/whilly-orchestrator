"""Unit tests for the v4.4 opencode + Groq default (m1-opencode-groq-default).

Pins three behavioural invariants:

1. ``whilly.agents.opencode.DEFAULT_MODEL == 'groq/openai/gpt-oss-120b'``
   — the new free-tier default since v4.4.
2. ``OpenCodeBackend.build_command()`` honours ``WHILLY_MODEL`` env
   override (no regression of the existing override behaviour).
3. ``check_opencode_groq_credentials()`` emits a single-line, actionable
   diagnostic when the operator selects the default groq path without
   ``GROQ_API_KEY`` set, and is a no-op when the operator opts out by
   pointing ``WHILLY_MODEL`` at any non-groq provider.

These pins back VAL-M1-AGENT-DEFAULT-001 / -002 / -003 in the M1
validation contract.
"""

from __future__ import annotations

import pytest

from whilly.agents.opencode import DEFAULT_MODEL, OpenCodeBackend
from whilly.cli.worker import check_opencode_groq_credentials


# ──────────────────────────────────────────────────────────────────────────────
# DEFAULT_MODEL constant
# ──────────────────────────────────────────────────────────────────────────────


def test_default_model_is_groq_gpt_oss_120b() -> None:
    """v4.4 default: free-tier on Groq, ~14k req/day.

    If this changes, downstream `.env.example`, docker-compose.demo.yml,
    and docs/Distributed-Setup.md must change in the same PR (the
    integration test pins those literal strings).
    """
    assert DEFAULT_MODEL == "groq/openai/gpt-oss-120b", (
        f"DEFAULT_MODEL drift: expected 'groq/openai/gpt-oss-120b', got {DEFAULT_MODEL!r}. "
        "Update .env.example, docker-compose.demo.yml, and docs/Distributed-Setup.md in lockstep."
    )


# ──────────────────────────────────────────────────────────────────────────────
# WHILLY_MODEL env override behaviour
# ──────────────────────────────────────────────────────────────────────────────


def test_default_model_used_when_whilly_model_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset ``WHILLY_MODEL`` resolves to the v4.4 Groq default."""
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    backend = OpenCodeBackend()
    cmd = backend.build_command("hi there")
    assert "groq/openai/gpt-oss-120b" in cmd, f"unset WHILLY_MODEL should yield Groq default; got {cmd!r}"


def test_whilly_model_override_anthropic_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ``WHILLY_MODEL=anthropic/claude-opus-4-6`` overrides the default.

    Backwards compat: operators on the Anthropic plan keep working by
    setting one env var, no code path change needed.
    """
    monkeypatch.setenv("WHILLY_MODEL", "anthropic/claude-opus-4-6")
    backend = OpenCodeBackend()
    cmd = backend.build_command("hi")
    assert "anthropic/claude-opus-4-6" in cmd
    assert "groq/openai/gpt-oss-120b" not in cmd


def test_whilly_model_override_groq_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """``provider/sub/model`` triple passes through normalize_model unchanged."""
    monkeypatch.setenv("WHILLY_MODEL", "groq/meta-llama/llama-3.1-70b-versatile")
    backend = OpenCodeBackend()
    cmd = backend.build_command("hi")
    assert "groq/meta-llama/llama-3.1-70b-versatile" in cmd


def test_whilly_model_override_bare_id_auto_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ids (no ``/``) still get auto-prefixed by the heuristic table.

    The auto-prefix logic (claude→anthropic, gpt→openai, …) must keep
    working for back-compat with operators who set bare ids.
    """
    monkeypatch.setenv("WHILLY_MODEL", "claude-opus-4-6")
    backend = OpenCodeBackend()
    cmd = backend.build_command("hi")
    # Auto-prefix table maps "claude*" → anthropic.
    assert "anthropic/claude-opus-4-6" in cmd


# ──────────────────────────────────────────────────────────────────────────────
# check_opencode_groq_credentials — fail-fast guard
# ──────────────────────────────────────────────────────────────────────────────


def test_groq_check_passes_when_cli_not_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other CLI selectors are not the groq guard's concern.

    ``WHILLY_CLI=claude-code`` or ``gemini`` etc. → return None even if
    ``GROQ_API_KEY`` is unset.
    """
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WHILLY_CLI", "claude-code")
    monkeypatch.setenv("WHILLY_MODEL", "anthropic/claude-opus-4-6")
    assert check_opencode_groq_credentials() is None


def test_groq_check_passes_when_cli_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("WHILLY_CLI", raising=False)
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    assert check_opencode_groq_credentials() is None


def test_groq_check_passes_when_model_is_non_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    """``WHILLY_MODEL=anthropic/...`` opts out of the groq path even with opencode CLI."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "anthropic/claude-opus-4-6")
    assert check_opencode_groq_credentials() is None


def test_groq_check_passes_when_model_is_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "openai/gpt-4o-mini")
    assert check_opencode_groq_credentials() is None


def test_groq_check_passes_when_api_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_test_key_for_unit_test_only")
    assert check_opencode_groq_credentials() is None


def test_groq_check_fails_when_api_key_missing_and_model_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path (model unset → groq) without GROQ_API_KEY must return the diagnostic."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    msg = check_opencode_groq_credentials()
    assert msg is not None
    # Single-line diagnostic so docker-compose / CI grep assertions are simple.
    assert "\n" not in msg, f"diagnostic must be single-line; got multi-line: {msg!r}"
    assert "GROQ_API_KEY is required" in msg
    assert "https://console.groq.com" in msg
    assert "WHILLY_CLI=opencode" in msg


def test_groq_check_fails_when_api_key_missing_and_model_explicit_groq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``WHILLY_MODEL=groq/...`` without API key must also fail."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "groq/openai/gpt-oss-120b")
    msg = check_opencode_groq_credentials()
    assert msg is not None
    assert "GROQ_API_KEY is required" in msg


def test_groq_check_fails_when_api_key_is_only_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / whitespace-only GROQ_API_KEY counts as missing."""
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("GROQ_API_KEY", "   ")
    msg = check_opencode_groq_credentials()
    assert msg is not None
    assert "GROQ_API_KEY is required" in msg


def test_groq_check_case_insensitive_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """``WHILLY_CLI=OPENCODE`` (uppercase) still triggers the guard."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WHILLY_CLI", "OPENCODE")
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    msg = check_opencode_groq_credentials()
    assert msg is not None
    assert "GROQ_API_KEY is required" in msg
