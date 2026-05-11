"""No-op notifier — used when the Slack feature is disabled or unconfigured.

Returned by :func:`whilly.adapters.notifications.factory.make_notifier`
whenever ``SLACK_ENABLED`` is false, the access token is empty, or the
channel id is empty. Keeping the no-op as its own concrete type (instead
of ``None``) lets the composition root call ``notifier.notify_run_completed``
unconditionally — the type system, not a runtime ``if``, witnesses that
the port is satisfied.
"""

from __future__ import annotations

from whilly.core.notifications import NotificationPort, RunCompletedEvent


class NullNotifier(NotificationPort):
    """Discards every event. Implements :class:`NotificationPort`."""

    def notify_run_completed(self, event: RunCompletedEvent) -> None:
        del event  # intentionally discarded


__all__ = ["NullNotifier"]
