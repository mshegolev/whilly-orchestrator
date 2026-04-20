"""Light-touch workflow → board sync helper.

This is *not* the ADR-015 Syncer (background thread tailing
``whilly_events.jsonl``). It's a synchronous function pipeline scripts call
directly — "I just finished the picked_up stage, move the card" — wired
once per stage transition.

Rationale: the event-bus Syncer requires a process lifecycle discussion
(daemon? inline listener? CI-friendly flushing?) that deserves its own
ADR. Meanwhile, self-hosting scripts need board movement *today*. This
helper ships the minimum-correct surface so those scripts don't each
reinvent "look up mapping, resolve status id, call move_item" boilerplate.

Keeping the signature stable means the eventual Syncer can call this same
helper internally — no double-migration for callers.

Typical use::

    from whilly.workflow.sync import move_on_event, load_or_none

    mapping = load_or_none()
    board = get_board("github_project", url=os.environ["WHILLY_PROJECT_URL"])
    move_on_event(board, mapping, "acme/repo#42", "picked_up")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from whilly.workflow.analyzer import load_mapping
from whilly.workflow.base import LifecycleEvent, WorkflowMapping

if TYPE_CHECKING:
    from whilly.workflow.base import BoardSink

log = logging.getLogger("whilly.workflow.sync")


def load_or_none(path=None) -> WorkflowMapping | None:
    """Thin passthrough to :func:`whilly.workflow.analyzer.load_mapping` —
    exposed here so scripts don't need to reach into the analyzer module
    for this one call."""
    return load_mapping(path)


def move_on_event(
    board: "BoardSink | None",
    mapping: WorkflowMapping | None,
    issue_ref: str,
    event: str | LifecycleEvent,
) -> bool:
    """Move the card for *issue_ref* to the status mapped to *event*.

    Graceful-no-op discipline: every "cannot move" condition returns
    ``False`` and logs — callers never have to wrap this in try/except.
    Specifically, we skip when:

    * *board* is ``None`` — workflow integration disabled for this run
    * *mapping* is ``None`` — user hasn't run ``--workflow-analyze`` yet
    * the event isn't in the mapping — user chose [S]kip in the proposer
    * the mapped status name doesn't resolve to a live board status —
      someone renamed the column after the mapping was frozen

    Returns ``True`` only when the board actually acknowledged the move.
    """
    if board is None:
        return False
    if mapping is None:
        log.debug("no workflow mapping loaded — skipping move for %s/%s", issue_ref, event)
        return False

    status_name = mapping.status_for(event)
    if not status_name:
        log.debug("event %r has no mapped status — skipping move for %s", str(event), issue_ref)
        return False

    try:
        statuses = board.list_statuses()
    except RuntimeError as exc:
        log.warning("could not list board statuses: %s", exc)
        return False

    target = next((s for s in statuses if s.name == status_name), None)
    if target is None:
        # Case-insensitive fallback — columns get renamed slightly, don't wipe
        # on a capitalisation change.
        lower = status_name.lower()
        target = next((s for s in statuses if s.name.lower() == lower), None)
    if target is None:
        log.warning(
            "mapped status %r not found on board — mapping may be stale (event=%s, issue=%s)",
            status_name,
            event,
            issue_ref,
        )
        return False

    return bool(board.move_item(issue_ref, target))
