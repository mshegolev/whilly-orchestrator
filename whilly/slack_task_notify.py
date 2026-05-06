"""Best-effort Slack webhook notifications for per-task demo runs.

The production notification port reports whole-run completion through
``chat.postMessage``. Demo videos need a smaller signal: one Slack message
per task as workers claim and finish rows. This module intentionally uses
Incoming Webhooks so the demo only needs one env var and never blocks the
state machine when Slack is slow or unavailable.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Callable, Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from whilly.adapters.runner.result_parser import AgentResult
from whilly.llm_ops import LLMOpsSession

log = logging.getLogger(__name__)

WEBHOOK_ENV: Final[str] = "WHILLY_SLACK_WEBHOOK_URL"
LEGACY_WEBHOOK_ENV: Final[str] = "SLACK_WEBHOOK_URL"
TOKEN_ENV: Final[str] = "WHILLY_SLACK_ACCESS_TOKEN"
LEGACY_TOKEN_ENV: Final[str] = "SLACK_ACCESS_TOKEN"
CHANNEL_ENV: Final[str] = "WHILLY_SLACK_CHANNEL"
LEGACY_CHANNEL_ENV: Final[str] = "SLACK_CHANNEL"
CHANNEL_URL_ENV: Final[str] = "WHILLY_SLACK_CHANNEL_URL"
LEGACY_CHANNEL_URL_ENV: Final[str] = "SLACK_CHANNEL_URL"
API_BASE_URL_ENV: Final[str] = "WHILLY_SLACK_API_BASE_URL"
ENABLED_ENV: Final[str] = "WHILLY_SLACK_ENABLED"
EVENTS_ENV: Final[str] = "WHILLY_SLACK_NOTIFY_EVENTS"
PUBLIC_BASE_URL_ENV: Final[str] = "WHILLY_PUBLIC_BASE_URL"
TIMEOUT_ENV: Final[str] = "WHILLY_SLACK_TIMEOUT_S"

DEFAULT_DEMO_CHANNEL_ID: Final[str] = "C0B1WT58EBE"
_DEFAULT_EVENTS: Final[str] = "terminal"
_DEFAULT_PUBLIC_BASE_URL: Final[str] = "http://127.0.0.1:8000"
_DEFAULT_API_BASE_URL: Final[str] = "https://slack.com/api"
_DEFAULT_TIMEOUT_S: Final[float] = 5.0

TaskSlackPost = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any] | None]


def urllib_slack_post(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_s: float,
) -> Mapping[str, Any] | None:
    """POST a Slack payload using stdlib urllib."""

    body = json.dumps(dict(payload)).encode("utf-8")
    request = Request(url, data=body, method="POST")
    for name, value in headers.items():
        request.add_header(name, value)
    with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - URL is deployment config
        raw = response.read()
    try:
        decoded = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _webhook_url() -> str | None:
    value = os.environ.get(WEBHOOK_ENV) or os.environ.get(LEGACY_WEBHOOK_ENV)
    return value.strip() if value and value.strip() else None


def _access_token() -> str | None:
    value = os.environ.get(TOKEN_ENV) or os.environ.get(LEGACY_TOKEN_ENV)
    return value.strip() if value and value.strip() else None


def _channel_from_url(value: str) -> str | None:
    parts = [part for part in value.split("/") if part]
    for part in reversed(parts):
        candidate = part.split("?", 1)[0]
        if candidate.startswith(("C", "G", "D")) and len(candidate) >= 9:
            return candidate
    return None


def _channel_id() -> str:
    configured = os.environ.get(CHANNEL_ENV) or os.environ.get(LEGACY_CHANNEL_ENV)
    if configured and configured.strip():
        return _channel_from_url(configured) or configured.strip()
    channel_url = os.environ.get(CHANNEL_URL_ENV) or os.environ.get(LEGACY_CHANNEL_URL_ENV)
    if channel_url:
        parsed = _channel_from_url(channel_url)
        if parsed:
            return parsed
    return DEFAULT_DEMO_CHANNEL_ID


def _api_base_url() -> str:
    return (os.environ.get(API_BASE_URL_ENV) or _DEFAULT_API_BASE_URL).rstrip("/")


def _configured_events() -> set[str]:
    raw = os.environ.get(EVENTS_ENV, _DEFAULT_EVENTS).lower()
    return {part.strip() for part in raw.replace(",", " ").split() if part.strip()}


def _enabled() -> bool:
    raw = os.environ.get(ENABLED_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "none", "off"}


def _should_emit(event: str) -> bool:
    if not _enabled():
        return False
    configured = _configured_events()
    if not configured or configured & {"0", "false", "no", "none", "off"}:
        return False
    if "all" in configured or event in configured:
        return True
    if event in {"done", "failed", "cancelled"} and "terminal" in configured:
        return True
    return event == "started" and "start" in configured


def _timeout_s() -> float:
    try:
        return float(os.environ.get(TIMEOUT_ENV, str(_DEFAULT_TIMEOUT_S)))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _task_url(session: LLMOpsSession) -> str:
    base_url = (os.environ.get(PUBLIC_BASE_URL_ENV) or _DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    return f"{base_url}/llm-ops?task_id={quote(session.task_id, safe='')}"


def _one_line(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _post_text(text: str, *, http_post: TaskSlackPost = urllib_slack_post) -> None:
    webhook_url = _webhook_url()
    expect_api_ok = False
    if webhook_url is not None:
        url = webhook_url
        headers = {"Content-Type": "application/json; charset=utf-8"}
        payload = {"text": text}
    else:
        token = _access_token()
        if token is None:
            return
        url = f"{_api_base_url()}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {"channel": _channel_id(), "text": text}
        expect_api_ok = True
    try:
        response = http_post(url, headers, payload, _timeout_s())
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        log.warning("slack task notify: transport error: %s", exc)
        return
    except Exception:  # pragma: no cover - observability must never break workers
        log.warning("slack task notify: unexpected transport error", exc_info=True)
        return
    if expect_api_ok and isinstance(response, Mapping) and not response.get("ok"):
        log.warning("slack task notify: api error %r", response.get("error"))


def _model_part(session: LLMOpsSession) -> str:
    if session.model:
        if session.model.startswith(f"{session.provider}/"):
            return session.model
        return f"{session.provider}/{session.model}"
    return session.provider


def notify_slack_task_started(
    session: LLMOpsSession,
    *,
    http_post: TaskSlackPost = urllib_slack_post,
) -> None:
    """Send a best-effort Slack message when a worker starts a task."""

    if not _should_emit("started"):
        return
    text = (
        f":hourglass_flowing_sand: Whilly STARTED {session.task_id} "
        f"plan={session.plan_id} worker={session.worker_id} model={_model_part(session)} "
        f"attempt={session.attempt} logs={_task_url(session)}"
    )
    _post_text(text, http_post=http_post)


def notify_slack_task_terminal(
    session: LLMOpsSession,
    status: str,
    result: AgentResult | None = None,
    *,
    reason: str | None = None,
    http_post: TaskSlackPost = urllib_slack_post,
) -> None:
    """Send a best-effort Slack message after a task reaches a terminal state."""

    event = status.lower()
    if not _should_emit(event):
        return
    icon = ":white_check_mark:" if event == "done" else ":x:"
    pieces = [
        f"{icon} Whilly {status.upper()} {session.task_id}",
        f"plan={session.plan_id}",
        f"worker={session.worker_id}",
        f"model={_model_part(session)}",
    ]
    if result is not None:
        pieces.append(f"exit_code={result.exit_code}")
        pieces.append(f"duration_ms={result.usage.duration_ms}")
        pieces.append(f"cost_usd={result.usage.cost_usd}")
    if reason:
        pieces.append(f"reason={_one_line(reason, limit=180)}")
    pieces.append(f"logs={_task_url(session)}")
    _post_text(" ".join(pieces), http_post=http_post)


__all__ = [
    "API_BASE_URL_ENV",
    "CHANNEL_ENV",
    "CHANNEL_URL_ENV",
    "DEFAULT_DEMO_CHANNEL_ID",
    "ENABLED_ENV",
    "EVENTS_ENV",
    "LEGACY_CHANNEL_ENV",
    "LEGACY_CHANNEL_URL_ENV",
    "LEGACY_TOKEN_ENV",
    "LEGACY_WEBHOOK_ENV",
    "PUBLIC_BASE_URL_ENV",
    "TOKEN_ENV",
    "TIMEOUT_ENV",
    "WEBHOOK_ENV",
    "TaskSlackPost",
    "notify_slack_task_started",
    "notify_slack_task_terminal",
    "urllib_slack_post",
]
