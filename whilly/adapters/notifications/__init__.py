"""Outbound-notification adapters (Slack, Null) for :class:`NotificationPort`.

Mirrors the shape of :mod:`whilly.adapters.db`: the port lives in
:mod:`whilly.core.notifications`, the concrete implementations live
here, and :func:`make_notifier` is the single producer used by the
composition root in :mod:`whilly.cli.run`.
"""

from whilly.adapters.notifications.factory import make_notifier
from whilly.adapters.notifications.null import NullNotifier
from whilly.adapters.notifications.slack import (
    HttpPost,
    HttpResponse,
    SlackNotifier,
    urllib_post,
)

__all__ = [
    "HttpPost",
    "HttpResponse",
    "NullNotifier",
    "SlackNotifier",
    "make_notifier",
    "urllib_post",
]
