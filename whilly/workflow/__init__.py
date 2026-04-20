"""Board workflow abstraction — pluggable project-board sinks.

Ships with one concrete implementation (:class:`~whilly.workflow.github.GitHubProjectBoard`)
but the entire surface is :class:`~whilly.workflow.base.BoardSink` Protocol-driven,
so Jira, Linear, GitLab, and in-house trackers drop in without touching callers.
Mirror of the :class:`whilly.agents.base.AgentBackend` design (OC-103) — same
factory/registry idiom, same "identity-equal re-export across shim layers"
discipline.

Public surface:

    from whilly.workflow import (
        BoardSink,            # Protocol
        LifecycleEvent,       # canonical event names (enum-ish)
        BoardStatus,          # (id, name) tuple for a board column/option
        WorkflowMapping,      # event -> status mapping loaded from .whilly/workflow.json
        GapReport,            # analyzer output (matched / missing / ambiguous)
        register_event,       # extend lifecycle vocabulary
        known_events,
        get_board,            # factory by name ("github_project" etc.)
        available_boards,
    )
"""

from __future__ import annotations

from whilly.workflow.base import (
    BoardSink,
    BoardStatus,
    GapReport,
    LifecycleEvent,
    WorkflowMapping,
)
from whilly.workflow.github import GitHubProjectBoard
from whilly.workflow.registry import (
    CORE_EVENTS,
    known_events,
    register_event,
)

__all__ = [
    "BoardSink",
    "BoardStatus",
    "GapReport",
    "LifecycleEvent",
    "WorkflowMapping",
    "GitHubProjectBoard",
    "CORE_EVENTS",
    "known_events",
    "register_event",
    "get_board",
    "available_boards",
]


_BOARD_REGISTRY: dict[str, type[BoardSink]] = {
    "github_project": GitHubProjectBoard,
}


def available_boards() -> list[str]:
    """Names of board implementations registered in this build."""
    return sorted(_BOARD_REGISTRY.keys())


def get_board(name: str, **kwargs) -> BoardSink:
    """Resolve a board impl by name, returning an instance bound to *kwargs*.

    Args:
        name: one of :func:`available_boards`. Case-insensitive.
        kwargs: forwarded to the impl constructor (URL, repo, etc.).

    Raises:
        ValueError: when *name* is unknown.
    """
    key = (name or "").strip().lower()
    if key not in _BOARD_REGISTRY:
        raise ValueError(f"Unknown board {name!r}. Available: {', '.join(available_boards())}")
    return _BOARD_REGISTRY[key](**kwargs)
