"""Onboarding contract: a fresh user with zero keys must be able to start
the demo with the built-in default model (m1-opencode-big-pickle-default).

This test pins the v4.4.2 zero-key default path: with `WHILLY_CLI=opencode`
and EVERY known provider credential cleared (`GROQ_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENCODE_API_KEY`,
`OPENCODE_ZEN_API_KEY`), the worker's pre-flight credential guard MUST
return `None` — i.e. no error, no `sys.exit`, no fail-fast diagnostic.

Backs VAL-M1-AGENT-DEFAULT-005.
"""

from __future__ import annotations

import pytest

from whilly.agents.opencode import DEFAULT_MODEL, OpenCodeBackend
from whilly.cli.worker import check_opencode_groq_credentials


_PROVIDER_KEYS_TO_CLEAR = (
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
)


def _clear_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROVIDER_KEYS_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)


def test_zero_key_default_does_not_raise_credentials_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh user contract: WHILLY_CLI=opencode + no model + no keys → no error."""
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    assert check_opencode_groq_credentials() is None, (
        "Zero-key onboarding regression: empty WHILLY_MODEL must NOT trigger the groq guard "
        "(default is now opencode/big-pickle)."
    )


def test_zero_key_default_with_explicit_big_pickle_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit `WHILLY_MODEL=opencode/big-pickle` is also zero-key."""
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "opencode/big-pickle")
    assert check_opencode_groq_credentials() is None


def test_zero_key_default_resolves_model_to_big_pickle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opencode backend, with no env, must resolve to the zero-key default
    so the agent invocation actually targets big-pickle."""
    _clear_all_keys(monkeypatch)
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    backend = OpenCodeBackend()
    cmd = backend.build_command("hello")
    assert DEFAULT_MODEL == "opencode/big-pickle"
    assert "opencode/big-pickle" in cmd


def test_explicit_groq_without_key_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the no-regression baseline for the explicit groq escape hatch."""
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "groq/openai/gpt-oss-120b")
    msg = check_opencode_groq_credentials()
    assert msg is not None
    assert "GROQ_API_KEY is required" in msg
