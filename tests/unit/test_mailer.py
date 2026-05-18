"""Unit tests for :class:`whilly.api.mailer.Mailer`.

PRD-post-auth-hardening §Epic C, Item 12. Pins the two transport branches
(SMTP vs event-log fallback) and the fail-open behaviour on SMTP error.
``aiosmtplib.send`` is monkeypatched in every test so no real network is
touched — the tests assert on the kwargs passed to the patched callable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from whilly.api import mailer
from whilly.api.mailer import Mailer


@pytest.fixture(autouse=True)
def _isolate_smtp_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear SMTP env state and redirect the event log to tmp_path."""
    for var in (
        mailer.SMTP_HOST_ENV,
        mailer.SMTP_PORT_ENV,
        mailer.SMTP_USER_ENV,
        mailer.SMTP_PASSWORD_ENV,
        mailer.SMTP_FROM_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WHILLY_EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))


# ─── Branch A: SMTP unset → event-log fallback ──────────────────────────────


@pytest.mark.asyncio
async def test_send_magic_link_falls_back_to_event_log_when_smtp_unset(
    tmp_path: Path,
) -> None:
    """No WHILLY_SMTP_HOST → returns 'event_log', writes a JSONL entry."""
    m = Mailer()
    assert m.smtp_configured is False
    transport = await m.send_magic_link(
        email="alice@example.com",
        magic_url="http://127.0.0.1:8000/auth/magic?token=abc",
        expires_at_unix=1_700_000_000,
    )
    assert transport == "event_log"
    log = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    rec = json.loads(log.strip())
    assert rec["event_type"] == "auth.magic_link.sent"
    assert rec["email"] == "alice@example.com"
    assert rec["transport"] == "event_log"
    assert rec["fallback_reason"] == "no_smtp_host"


# ─── Branch B: SMTP configured → aiosmtplib.send invoked ────────────────────


@pytest.mark.asyncio
async def test_send_magic_link_invokes_aiosmtplib_when_smtp_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHILLY_SMTP_HOST set → returns 'smtp', aiosmtplib.send awaited once."""
    pytest.importorskip("aiosmtplib")
    monkeypatch.setenv(mailer.SMTP_HOST_ENV, "smtp.example.com")
    monkeypatch.setenv(mailer.SMTP_PORT_ENV, "2525")
    monkeypatch.setenv(mailer.SMTP_USER_ENV, "u")
    monkeypatch.setenv(mailer.SMTP_PASSWORD_ENV, "p")
    monkeypatch.setenv(mailer.SMTP_FROM_ENV, "from@example.com")

    captured: dict[str, Any] = {}

    async def _fake_send(msg: Any, **kwargs: Any) -> None:
        captured["msg"] = msg
        captured["kwargs"] = kwargs

    import aiosmtplib

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)

    m = Mailer()
    assert m.smtp_configured is True
    transport = await m.send_magic_link(
        email="alice@example.com",
        magic_url="http://127.0.0.1:8000/auth/magic?token=abc",
        expires_at_unix=1_700_000_000,
    )
    assert transport == "smtp"
    assert captured["kwargs"]["hostname"] == "smtp.example.com"
    assert captured["kwargs"]["port"] == 2525
    assert captured["kwargs"]["username"] == "u"
    assert captured["kwargs"]["password"] == "p"
    msg = captured["msg"]
    assert msg["To"] == "alice@example.com"
    assert msg["From"] == "from@example.com"
    assert msg["Subject"]


# ─── Branch C: SMTP error → fail-open to event log ──────────────────────────


@pytest.mark.asyncio
async def test_send_magic_link_smtp_error_falls_back_to_event_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """aiosmtplib.send raises → mailer logs warning, writes event, returns event_log."""
    pytest.importorskip("aiosmtplib")
    monkeypatch.setenv(mailer.SMTP_HOST_ENV, "smtp.example.com")
    import aiosmtplib

    monkeypatch.setattr(
        aiosmtplib,
        "send",
        AsyncMock(side_effect=ConnectionRefusedError("relay unreachable")),
    )
    m = Mailer()
    transport = await m.send_magic_link(
        email="bob@example.com",
        magic_url="http://x/auth/magic?token=t",
        expires_at_unix=42,
    )
    assert transport == "event_log"
    log = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    rec = json.loads(log.strip())
    assert rec["event_type"] == "auth.magic_link.sent"
    assert rec["fallback_reason"] == "smtp_error"


# ─── Auxiliary: port parsing tolerates malformed values ─────────────────────


def test_mailer_defaults_port_to_587_on_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mailer.SMTP_HOST_ENV, "smtp.example.com")
    m = Mailer()
    assert m._port == mailer.DEFAULT_SMTP_PORT


def test_mailer_defaults_port_to_587_on_malformed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mailer.SMTP_HOST_ENV, "smtp.example.com")
    monkeypatch.setenv(mailer.SMTP_PORT_ENV, "not-an-int")
    m = Mailer()
    assert m._port == mailer.DEFAULT_SMTP_PORT


# ─── Default From synthesis ─────────────────────────────────────────────────


def test_mailer_default_from_uses_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mailer.SMTP_HOST_ENV, "smtp.example.com")
    m = Mailer()
    # Just verify the shape — actual hostname value depends on the test machine.
    assert m._from.startswith("whilly@")


# ─── Event-log fallback handles a missing parent directory ──────────────────


@pytest.mark.asyncio
async def test_event_log_fallback_creates_parent_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Mailer's mkdir parents=True keeps the fallback durable even when
    the event-log directory doesn't exist yet.
    """
    deep_path = tmp_path / "a" / "b" / "c" / "events.jsonl"
    monkeypatch.setenv("WHILLY_EVENT_LOG_PATH", str(deep_path))
    monkeypatch.delenv(mailer.SMTP_HOST_ENV, raising=False)
    m = Mailer()
    transport = await m.send_magic_link(email="x@example.com", magic_url="http://x/m?t=1", expires_at_unix=1)
    assert transport == "event_log"
    assert deep_path.exists()
