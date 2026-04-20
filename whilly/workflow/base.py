"""BoardSink Protocol + shared dataclasses for project-board integration.

The :class:`BoardSink` Protocol is the stable contract every tracker adapter
must implement (GitHub Projects v2, Jira, Linear, …). Keeping this surface
*narrow* (list statuses, add status, move item) is deliberate — rich features
like sprint metadata or custom fields are extension territory, not v1.

Shape parity with :mod:`whilly.agents.base` on purpose: same Protocol-plus-
dataclasses idiom, same strict "impls never raise on transport errors — they
return a Result with an error code" discipline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


# ── Canonical lifecycle event vocabulary ──────────────────────────────────────


class LifecycleEvent(str, Enum):
    """The fixed core vocabulary every whilly pipeline understands.

    Kept as a ``str`` Enum so it round-trips through JSON/CLI without
    manual conversion. Additional custom events can be registered via
    :func:`whilly.workflow.register_event` — those live in the registry,
    not this Enum (the Enum is the *guaranteed* surface).
    """

    READY = "ready"  # issue labelled whilly:ready, not yet picked
    PICKED_UP = "picked_up"  # whilly claimed it, agent running
    IN_REVIEW = "in_review"  # PR opened, awaiting human / reviewer agent
    DONE = "done"  # PR merged / issue closed successfully
    REFUSED = "refused"  # Decision Gate said no
    FAILED = "failed"  # budget / timeout / test gate / auth error


# ── Status representation ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class BoardStatus:
    """One column/option on a project board.

    ``id`` is the tracker-native identifier (GraphQL node id for GitHub,
    transition id for Jira, ...). ``name`` is the human label shown to
    the user — what we fuzzy-match against.
    """

    id: str
    name: str

    def __str__(self) -> str:
        return self.name


# ── Mapping loaded from .whilly/workflow.json ────────────────────────────────


@dataclass
class WorkflowMapping:
    """Event → status mapping, plus board-identity info.

    Serialised shape (JSON in ``.whilly/workflow.json``)::

        {
          "version": 1,
          "board": {"kind": "github_project", "url": "..."},
          "events": {
            "ready":     "Todo",
            "picked_up": "In Progress",
            ...
          },
          "aliases": {
            "ready":     ["todo", "backlog"],
            ...
          }
        }

    Fields:
        board_kind: registry key for :func:`whilly.workflow.get_board`.
        board_url: tracker-native URL (GitHub project URL, Jira board URL, ...).
        events: event-name → status-name mapping (strings — status id is
            resolved at runtime from the live board).
        aliases: event-name → list of synonym strings for fuzzy matching.
            Optional — sensible defaults live in the mapper module.
    """

    board_kind: str
    board_url: str
    events: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    version: int = 1

    def status_for(self, event: str | LifecycleEvent) -> str | None:
        """Return the mapped status name for *event*, or ``None`` if unmapped
        (caller decides whether to skip, warn, or auto-propose)."""
        key = event.value if isinstance(event, LifecycleEvent) else str(event)
        return self.events.get(key)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "board": {"kind": self.board_kind, "url": self.board_url},
            "events": dict(self.events),
            "aliases": {k: list(v) for k, v in self.aliases.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowMapping":
        board = data.get("board") or {}
        return cls(
            board_kind=board.get("kind") or "",
            board_url=board.get("url") or "",
            events=dict(data.get("events") or {}),
            aliases={k: list(v) for k, v in (data.get("aliases") or {}).items()},
            version=int(data.get("version") or 1),
        )


# ── Analyzer output ───────────────────────────────────────────────────────────


@dataclass
class GapReport:
    """Result of comparing registered lifecycle events to live board statuses.

    Each event lands in exactly one bucket:

    - ``matched[event] = BoardStatus`` — a unique good match was found.
    - ``missing`` list of events — no alias matched any board status.
    - ``ambiguous[event] = list[BoardStatus]`` — multiple matches; the
      proposer prompts the user to pick one.

    ``board_statuses`` is the raw list of statuses we got from the board —
    kept so downstream (proposer, CLI output) can show it without re-querying.
    """

    board_url: str
    board_statuses: list[BoardStatus]
    matched: dict[str, BoardStatus] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[BoardStatus]] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        """True when every registered event has a unique match."""
        return not self.missing and not self.ambiguous


# ── The Protocol every board adapter implements ──────────────────────────────


class BoardSink(Protocol):
    """Stable contract for project-board adapters.

    Implementations must:

    * Expose ``kind`` — the registry key used by
      :func:`whilly.workflow.get_board`.
    * Never raise on transport errors during :meth:`move_item` — return
      ``False`` instead. :meth:`list_statuses` may raise :class:`RuntimeError`
      on auth/setup problems (analyzer expects to fail fast there).
    * Treat lookups as best-effort — missing items / unknown statuses are
      logged and skipped, never crash the loop.
    """

    kind: str

    def list_statuses(self) -> list[BoardStatus]:
        """Return every status option on the board's status field.

        Raises:
            RuntimeError: board not reachable, authentication missing, or
                no status field exists.
        """
        ...

    def add_status(self, name: str) -> BoardStatus:
        """Create a new status option on the board and return it.

        May raise :class:`NotImplementedError` when the tracker doesn't
        support creating statuses programmatically (Jira classic) — the
        proposer surfaces the error and falls back to map-existing.
        """
        ...

    def move_item(self, issue_ref: str, status: BoardStatus) -> bool:
        """Move the board item representing *issue_ref* to *status*.

        *issue_ref* is the tracker-native reference — for GitHub, the issue
        URL or ``owner/repo#N`` form; for Jira, the issue key. Adapters
        parse whatever form they support.

        Returns ``True`` on success, ``False`` on any failure (item not
        found, permission denied, network). Never raises.
        """
        ...
