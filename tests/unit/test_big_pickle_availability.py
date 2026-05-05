"""Unit tests for ``check_opencode_big_pickle_availability`` — the
forward-compatibility safety net for the v4.4.2 zero-key default
(misc-m1-big-pickle-sunset-watch).

Why the helper exists
---------------------
OpenCode Zen documents Big Pickle as free "for a limited time". When
Zen sunsets the free tier, every worker that ships with the v4.4.2
default (``WHILLY_MODEL`` empty → ``opencode/big-pickle``) will fail
at the FIRST agent run with an unhelpful provider 401 — silently
breaking zero-key onboarding. This helper, gated behind
``WHILLY_BIG_PICKLE_HEALTHCHECK=1``, runs a one-shot probe at worker
startup and emits a multi-line stderr warning listing 3 escape hatches
when the probe comes back ``401`` / ``403`` / ``"requires API key"``.

What this file pins
-------------------
1. The helper is OFF by default — never runs subprocess unless the env
   flag is exactly ``"1"``.
2. The subprocess invocation uses the documented OpenCode Zen probe
   command line: ``opencode run --format json --model opencode/big-pickle 'ping'``
   with a 10-second timeout.
3. Healthy probe (return code 0, no auth markers in output) → returns
   ``None``; no warning emitted.
4. Auth-failure probe (401 / 403 / "api key required" anywhere in
   stdout/stderr or non-zero return code with the marker) → returns a
   multi-line warning string listing the 3 escape hatches with exact
   ``WHILLY_MODEL=...`` env lines for each.
5. Probe execution failures (``opencode`` missing from PATH,
   subprocess timeout) → returns ``None`` so the worker boots through —
   the helper is a safety net, not a hard gate.
6. The warning is multi-line, contains the three escape-hatch model
   ids verbatim (``groq/openai/gpt-oss-120b``,
   ``anthropic/claude-opus-4-6``, ``openai/gpt-4o-mini``) and the
   three corresponding API-key env-var names.

No integration test in this file hits the network — that's the
``.github/workflows/big-pickle-health.yml`` weekly cron's job.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest import mock

import pytest

from whilly.cli.worker import (
    BIG_PICKLE_HEALTHCHECK_ENV,
    check_opencode_big_pickle_availability,
)


# ---------------------------------------------------------------------------
# Subprocess result builder
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["opencode", "run", "--format", "json", "--model", "opencode/big-pickle", "ping"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Gating: the helper is OFF unless the env flag is exactly "1"
# ---------------------------------------------------------------------------


def test_helper_is_no_op_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BIG_PICKLE_HEALTHCHECK_ENV, raising=False)
    with mock.patch("subprocess.run") as runner:
        result = check_opencode_big_pickle_availability()
    assert result is None
    runner.assert_not_called()


def test_helper_is_no_op_when_env_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "0")
    with mock.patch("subprocess.run") as runner:
        result = check_opencode_big_pickle_availability()
    assert result is None
    runner.assert_not_called()


def test_helper_is_no_op_when_env_truthy_but_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "true")
    with mock.patch("subprocess.run") as runner:
        result = check_opencode_big_pickle_availability()
    assert result is None
    runner.assert_not_called()


# ---------------------------------------------------------------------------
# Healthy probe → None
# ---------------------------------------------------------------------------


def test_helper_returns_none_when_probe_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    healthy_payload = '{"role":"assistant","content":"pong"}'
    with mock.patch("subprocess.run", return_value=_completed(0, stdout=healthy_payload)) as runner:
        result = check_opencode_big_pickle_availability()
    assert result is None
    runner.assert_called_once()
    args, kwargs = runner.call_args
    cmd = args[0] if args else kwargs.get("args")
    assert cmd == [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "opencode/big-pickle",
        "ping",
    ]
    assert kwargs.get("timeout") == 10
    assert kwargs.get("text") is True
    assert kwargs.get("capture_output") is True


def test_helper_returns_none_when_probe_succeeds_with_whitespace_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / whitespace-only stdout with rc=0 must not be treated as auth failure."""
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    with mock.patch("subprocess.run", return_value=_completed(0, stdout="   \n")):
        result = check_opencode_big_pickle_availability()
    assert result is None


# ---------------------------------------------------------------------------
# Auth-failure probe → warning string
# ---------------------------------------------------------------------------


_AUTH_FAILURE_FIXTURES: list[tuple[str, dict[str, Any]]] = [
    (
        "stderr_401_token",
        {"returncode": 1, "stdout": "", "stderr": "opencode: HTTP 401 Unauthorized from provider"},
    ),
    (
        "stderr_403_token",
        {"returncode": 1, "stdout": "", "stderr": "got 403 Forbidden: trial expired"},
    ),
    (
        "stdout_api_key_required_phrase",
        {
            "returncode": 1,
            "stdout": '{"error":"API key required for opencode/big-pickle"}',
            "stderr": "",
        },
    ),
    (
        "stdout_unauthorized_phrase",
        {
            "returncode": 2,
            "stdout": '{"error":"unauthorized"}',
            "stderr": "",
        },
    ),
    (
        "stderr_requires_api_key_phrase",
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "this model requires API key — see https://opencode.zen",
        },
    ),
    (
        "case_insensitive_match",
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "AUTH FAILED: please provide an Api Key",
        },
    ),
]


@pytest.mark.parametrize("label,payload", _AUTH_FAILURE_FIXTURES, ids=[label for label, _ in _AUTH_FAILURE_FIXTURES])
def test_helper_returns_warning_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    with mock.patch("subprocess.run", return_value=_completed(**payload)):
        warning = check_opencode_big_pickle_availability()
    assert warning is not None, f"expected sunset warning for fixture {label!r}"
    assert "\n" in warning, "warning must be multi-line"
    # Three escape hatches must appear with both their env-var name AND
    # the WHILLY_MODEL value an operator can copy-paste verbatim.
    assert "GROQ_API_KEY" in warning
    assert "ANTHROPIC_API_KEY" in warning
    assert "OPENAI_API_KEY" in warning
    assert "WHILLY_MODEL=groq/openai/gpt-oss-120b" in warning
    assert "WHILLY_MODEL=anthropic/claude-opus-4-6" in warning
    assert "WHILLY_MODEL=openai/gpt-4o-mini" in warning


# ---------------------------------------------------------------------------
# Probe execution failures (binary missing, timeout) → None (graceful fall-through)
# ---------------------------------------------------------------------------


def test_helper_returns_none_when_opencode_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("opencode not on PATH")):
        result = check_opencode_big_pickle_availability()
    assert result is None


def test_helper_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    timeout_exc = subprocess.TimeoutExpired(cmd=["opencode"], timeout=10)
    with mock.patch("subprocess.run", side_effect=timeout_exc):
        result = check_opencode_big_pickle_availability()
    assert result is None


def test_helper_returns_none_on_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unexpected runtime error must NOT crash the worker boot."""
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    with mock.patch("subprocess.run", side_effect=OSError("permission denied")):
        result = check_opencode_big_pickle_availability()
    assert result is None


# ---------------------------------------------------------------------------
# Non-auth failure (rc != 0 but no auth markers) → None
# ---------------------------------------------------------------------------


def test_helper_returns_none_on_non_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-auth probe failures (network blip, opencode crash) must not trigger
    the sunset warning — only the explicit 401/403/API-key markers do.
    Otherwise we'd spam operators with false-positives every time their
    laptop is offline.
    """
    monkeypatch.setenv(BIG_PICKLE_HEALTHCHECK_ENV, "1")
    with mock.patch(
        "subprocess.run",
        return_value=_completed(1, stdout="", stderr="dial tcp: lookup api.opencode.zen: no such host"),
    ):
        result = check_opencode_big_pickle_availability()
    assert result is None
