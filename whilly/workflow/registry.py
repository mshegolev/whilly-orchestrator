"""Event registry for workflow lifecycle.

:class:`~whilly.workflow.base.LifecycleEvent` is the *guaranteed* core
vocabulary. This registry exists so downstream code (custom agents, workshop
participants, future whilly features like TRIZ challenge) can extend the
vocabulary without patching the Enum.

    from whilly.workflow import register_event

    register_event("triz_challenge", default_aliases=["challenge", "review"])

Registered events appear in :func:`known_events`, participate in the analyzer
gap report alongside core events, and can be mapped to board statuses in
``.whilly/workflow.json``.
"""

from __future__ import annotations

from whilly.workflow.base import LifecycleEvent


# Core events are always registered — their defaults live here so the analyzer
# and proposer can fuzzy-match without a mapping file present.
CORE_EVENTS: dict[str, list[str]] = {
    LifecycleEvent.READY.value: ["todo", "backlog", "ready", "open", "new", "triage"],
    LifecycleEvent.PICKED_UP.value: ["in progress", "in-progress", "wip", "doing", "active", "running"],
    LifecycleEvent.IN_REVIEW.value: ["in review", "review", "awaiting review", "pr", "pull request"],
    LifecycleEvent.DONE.value: ["done", "closed", "complete", "completed", "merged", "shipped"],
    LifecycleEvent.REFUSED.value: ["refused", "won't do", "wont do", "rejected", "needs clarification", "blocked"],
    LifecycleEvent.FAILED.value: ["failed", "error", "needs attention", "stuck", "deferred"],
}


_EVENTS: dict[str, list[str]] = dict(CORE_EVENTS)


def register_event(name: str, default_aliases: list[str] | None = None) -> None:
    """Add a custom lifecycle event to the registry.

    Idempotent — re-registering an existing name replaces its alias list
    (intentional: lets plugins update their own aliases at import time).

    Args:
        name: snake_case event identifier. Colliding with a
            :class:`~whilly.workflow.base.LifecycleEvent` value is allowed
            (updates aliases) but discouraged.
        default_aliases: substrings used by the fuzzy matcher to find this
            event's board status. Matching is case-insensitive.
    """
    if not name or not name.strip():
        raise ValueError("event name must be non-empty")
    _EVENTS[name.strip()] = list(default_aliases or [])


def known_events() -> dict[str, list[str]]:
    """Snapshot of currently registered events → alias lists.

    The returned dict is a copy — mutating it does NOT affect the registry.
    """
    return {k: list(v) for k, v in _EVENTS.items()}


def reset_to_core() -> None:
    """Discard custom registrations — useful in tests."""
    _EVENTS.clear()
    _EVENTS.update(CORE_EVENTS)
