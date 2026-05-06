"""Factory: :class:`whilly.config.WhillyConfig` → :class:`NotificationPort`.

The composition root in :mod:`whilly.cli.run` calls :func:`make_notifier`
once per ``whilly run`` invocation. The factory is the single place
that decides between :class:`SlackNotifier` (token + channel + enabled)
and :class:`NullNotifier` (anything else) — keeps the CLI free of
configuration logic and keeps the adapters free of env reads.
"""

from __future__ import annotations

import logging

from whilly.adapters.notifications.null import NullNotifier
from whilly.adapters.notifications.slack import SlackNotifier, urllib_post
from whilly.config import WhillyConfig
from whilly.core.notifications import MessageTemplate, NotificationPort


def make_notifier(
    cfg: WhillyConfig,
    logger: logging.Logger | None = None,
) -> NotificationPort:
    """Build the configured notifier.

    Returns :class:`NullNotifier` when the feature is disabled or the
    minimum config (token + channel) is incomplete — keeps the call
    site in :mod:`whilly.cli.run` ``if``-free.
    """
    log = logger if logger is not None else logging.getLogger("whilly")
    if not cfg.SLACK_ENABLED or not cfg.SLACK_ACCESS_TOKEN or not cfg.SLACK_CHANNEL:
        return NullNotifier()
    return SlackNotifier(
        token=cfg.SLACK_ACCESS_TOKEN,
        channel=cfg.SLACK_CHANNEL,
        api_base_url=cfg.SLACK_API_BASE_URL,
        timeout_s=cfg.SLACK_TIMEOUT_S,
        template=MessageTemplate(cfg.SLACK_MESSAGE_TEMPLATE),
        http_post=urllib_post,
        logger=log,
    )


__all__ = ["make_notifier"]
