"""Slack adapter for the run-completed notification port.

Posts to ``chat.postMessage`` with a bearer token; everything that varies
between deployments (token, channel, API base URL, timeout, message
template) flows in through the constructor from :class:`whilly.config.WhillyConfig`.
There are no string literals in the class body — :func:`make_notifier`
in :mod:`whilly.adapters.notifications.factory` is the only call site
that produces a :class:`SlackNotifier` and it sources every value from
the config.

Transport is stdlib :mod:`urllib.request` so the base install stays
``requests``-free (PRD SC-6 keeps the worker / control-plane import
closure tight). The HTTP call is reached through an injectable
``http_post`` callable so unit tests substitute a stub without
monkey-patching :mod:`urllib`.

Failure policy: every exception is swallowed and logged at WARNING.
The orchestrator's exit code MUST NOT change because Slack is down.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from whilly.core.notifications import (
    MessageTemplate,
    NotificationPort,
    RunCompletedEvent,
)


class HttpResponse(Protocol):
    """Minimal duck-typed response: just enough for the adapter to read JSON."""

    def json(self) -> Mapping[str, Any]: ...


HttpPost = Callable[
    [str, Mapping[str, str], Mapping[str, Any], float],
    HttpResponse,
]


@dataclass(frozen=True)
class _UrllibResponse:
    """Adapter-private response shim around an ``http.client.HTTPResponse`` body.

    We read the body once and parse JSON eagerly so the response object
    can be passed back through the ``HttpPost`` boundary as a simple
    value object. Slack returns 200 even on logical errors, so we don't
    need to expose the status code separately.
    """

    body: bytes

    def json(self) -> Mapping[str, Any]:
        try:
            decoded = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}


def urllib_post(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_s: float,
) -> HttpResponse:
    """Default :data:`HttpPost` impl — single POST via :mod:`urllib.request`.

    Lives at module scope (not inside :class:`SlackNotifier`) so the class
    body has zero HTTP-library coupling and tests inject a stub without
    importing :mod:`urllib` themselves.
    """
    body = json.dumps(dict(payload)).encode("utf-8")
    request = Request(url, data=body, method="POST")
    for name, value in headers.items():
        request.add_header(name, value)
    with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 — URL comes from config, not user input
        return _UrllibResponse(body=response.read())


@dataclass
class SlackNotifier(NotificationPort):
    """Posts a chat message to Slack via ``chat.postMessage``.

    All values flow in through the constructor; no env reads, no string
    literals. The factory in :mod:`whilly.adapters.notifications.factory`
    is the single producer.
    """

    token: str
    channel: str
    api_base_url: str
    timeout_s: float
    template: MessageTemplate
    http_post: HttpPost
    logger: logging.Logger

    def notify_run_completed(self, event: RunCompletedEvent) -> None:
        text = self.template.render(event)
        url = f"{self.api_base_url}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload: dict[str, Any] = {"channel": self.channel, "text": text}
        try:
            response = self.http_post(url, headers, payload, self.timeout_s)
        except (URLError, HTTPError, TimeoutError, OSError) as exc:
            self.logger.warning("slack notify: transport error: %s", exc)
            return
        except Exception:  # pragma: no cover — last-resort safety net
            self.logger.warning("slack notify: unexpected transport error", exc_info=True)
            return
        body = response.json() if response is not None else {}
        if not body.get("ok"):
            self.logger.warning("slack notify: api error %r", body.get("error"))


__all__ = [
    "HttpPost",
    "HttpResponse",
    "SlackNotifier",
    "urllib_post",
]
