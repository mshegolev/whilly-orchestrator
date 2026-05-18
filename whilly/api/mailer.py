"""SMTP magic-link transport (PRD-post-auth-hardening §Epic C, Item 12).

The :class:`Mailer` wraps the existing event-log-only magic-link delivery
path with an optional SMTP send via :mod:`aiosmtplib`. When SMTP isn't
configured (``WHILLY_SMTP_HOST`` empty or unset), the mailer transparently
falls back to writing the same ``auth.magic_link.sent`` event the v2
codebase wrote before — production deployments enable SMTP, dev / loopback
keeps the event-log behaviour so operators can copy the link from
``whilly_events.jsonl`` without standing up an SMTP relay.

Configuration (all env vars):

* ``WHILLY_SMTP_HOST``     — server hostname; **empty/unset disables SMTP**
* ``WHILLY_SMTP_PORT``     — integer, default ``587`` (STARTTLS submission)
* ``WHILLY_SMTP_USER``     — auth username (optional; relay may permit
                              anonymous on the LAN)
* ``WHILLY_SMTP_PASSWORD`` — auth password (optional)
* ``WHILLY_SMTP_FROM``     — From address; default
                              ``whilly@<hostname>`` synthesised from
                              :func:`socket.gethostname`

Async-only. Callers MUST ``await`` :meth:`Mailer.send_magic_link` —
synchronous SMTP in a coroutine event-loop blocks every other request
on the thread. ``aiosmtplib`` is a hard dependency added by this PR.

On any SMTP error (connection refused, auth failure, bad From, etc.) the
mailer logs a WARNING and falls back to the event-log path so the user-
facing auth flow still completes: the operator can recover the link
from the audit trail even when delivery is broken.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Env var names — kept module-level so tests and docs reference the same strings.
SMTP_HOST_ENV: Final[str] = "WHILLY_SMTP_HOST"
SMTP_PORT_ENV: Final[str] = "WHILLY_SMTP_PORT"
SMTP_USER_ENV: Final[str] = "WHILLY_SMTP_USER"
SMTP_PASSWORD_ENV: Final[str] = "WHILLY_SMTP_PASSWORD"
SMTP_FROM_ENV: Final[str] = "WHILLY_SMTP_FROM"

DEFAULT_SMTP_PORT: Final[int] = 587

# Reuse the existing event-log path constants from auth_routes so a single
# env var controls both code paths. Imported lazily to avoid a circular import
# (auth_routes uses Mailer; Mailer's fallback writes via the same module).
_EVENT_LOG_PATH_ENV: Final[str] = "WHILLY_EVENT_LOG_PATH"
_DEFAULT_EVENT_LOG_PATH: Final[str] = "whilly_logs/whilly_events.jsonl"


class Mailer:
    """Send magic-link emails via SMTP, or fall back to the event log.

    Resolves SMTP configuration from env at construction time. Re-read by
    calling ``Mailer()`` again (cheap — no connection is opened until the
    first ``send_magic_link``).
    """

    def __init__(self) -> None:
        host = (os.environ.get(SMTP_HOST_ENV) or "").strip()
        self._host: str | None = host or None
        # PORT is parsed defensively — a malformed value falls back to the
        # default rather than crashing startup.
        port_raw = (os.environ.get(SMTP_PORT_ENV) or "").strip()
        try:
            self._port: int = int(port_raw) if port_raw else DEFAULT_SMTP_PORT
        except ValueError:
            logger.warning("Mailer: invalid %s=%r; falling back to %d", SMTP_PORT_ENV, port_raw, DEFAULT_SMTP_PORT)
            self._port = DEFAULT_SMTP_PORT
        self._username: str | None = (os.environ.get(SMTP_USER_ENV) or "").strip() or None
        self._password: str | None = os.environ.get(SMTP_PASSWORD_ENV) or None
        self._from: str = (os.environ.get(SMTP_FROM_ENV) or "").strip() or self._default_from()

    @staticmethod
    def _default_from() -> str:
        """Synthesise a From address from the host name when one isn't set."""
        try:
            host = socket.gethostname() or "whilly.local"
        except OSError:
            host = "whilly.local"
        return f"whilly@{host}"

    @property
    def smtp_configured(self) -> bool:
        """True iff the mailer will attempt SMTP delivery rather than falling
        back to the event log. Useful for log lines that want to flag
        deployment posture (production vs dev)."""
        return self._host is not None

    async def send_magic_link(self, *, email: str, magic_url: str, expires_at_unix: int) -> str:
        """Deliver a magic-link email to ``email``.

        Returns the transport mode used: ``"smtp"`` or ``"event_log"``.

        Never raises. On any failure (SMTP error, missing aiosmtplib, etc.)
        falls back to writing the ``auth.magic_link.sent`` event to the
        event log so the link is recoverable even when delivery is broken.
        """
        if not self.smtp_configured:
            self._fallback_event(
                email=email, magic_url=magic_url, expires_at_unix=expires_at_unix, reason="no_smtp_host"
            )
            return "event_log"
        try:
            await self._send_via_smtp(email=email, magic_url=magic_url, expires_at_unix=expires_at_unix)
        except Exception:  # noqa: BLE001 — fail-open onto event-log path
            logger.warning(
                "Mailer: aiosmtplib send to %r failed; falling back to event log",
                email,
                exc_info=True,
            )
            self._fallback_event(email=email, magic_url=magic_url, expires_at_unix=expires_at_unix, reason="smtp_error")
            return "event_log"
        return "smtp"

    async def _send_via_smtp(self, *, email: str, magic_url: str, expires_at_unix: int) -> None:
        """Build the multipart message and hand it off to :mod:`aiosmtplib`."""
        try:
            import aiosmtplib  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "Mailer: aiosmtplib is not installed; add it to "
                "`pip install whilly-orchestrator[server]` or unset WHILLY_SMTP_HOST."
            ) from exc

        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = email
        msg["Subject"] = "Your Whilly sign-in link"
        plain = (
            "Click the link below to sign in to Whilly:\n\n"
            f"{magic_url}\n\n"
            f"Link expires at unix timestamp {expires_at_unix}.\n"
            "If you didn't request this, you can ignore this email."
        )
        html = (
            "<html><body><p>Click the link below to sign in to Whilly:</p>"
            f'<p><a href="{magic_url}">{magic_url}</a></p>'
            f"<p>Link expires at unix timestamp {expires_at_unix}.</p>"
            "<p>If you didn't request this, you can ignore this email.</p>"
            "</body></html>"
        )
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
        await aiosmtplib.send(
            msg,
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            start_tls=True,
        )

    def _fallback_event(self, *, email: str, magic_url: str, expires_at_unix: int, reason: str) -> None:
        """Write the same ``auth.magic_link.sent`` event the v2 codebase wrote."""
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": "auth.magic_link.sent",
            "email": email,
            "magic_link_url": magic_url,
            "expires_at_unix": expires_at_unix,
            "transport": "event_log",
            "fallback_reason": reason,
        }
        log_path = Path(os.environ.get(_EVENT_LOG_PATH_ENV, _DEFAULT_EVENT_LOG_PATH))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            # Last-resort: if even the event log is unwritable, log a warning
            # but keep the request flow alive. The user just won't get the link.
            logger.warning("Mailer: event-log fallback append failed (%s): %s", log_path, exc)


__all__ = [
    "DEFAULT_SMTP_PORT",
    "Mailer",
    "SMTP_FROM_ENV",
    "SMTP_HOST_ENV",
    "SMTP_PASSWORD_ENV",
    "SMTP_PORT_ENV",
    "SMTP_USER_ENV",
]
