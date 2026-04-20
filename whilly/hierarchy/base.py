"""Three-level work hierarchy — tracker-agnostic primitives.

Whilly operates on three canonical levels regardless of which tracker (GitHub,
Jira, Linear, GitLab, …) the work actually lives in:

    Epic   — strategic intent, business need. One → N Stories.
    Story  — concrete feature or capability. One → N Tasks.
    Task   — atomic unit, executable by a single agent. One → one PR.

Each tracker adapter maps these to its native concepts (GitHub: draft item /
issue / sub-issue; Jira: Epic / Story / Sub-task; Linear: project / issue /
sub-issue). The :class:`HierarchyAdapter` Protocol below is the stable
contract every adapter implements.

The design mirrors :class:`whilly.agents.base.AgentBackend`,
:class:`whilly.workflow.base.BoardSink`, and :class:`whilly.quality.base.QualityGate`:

* narrow Protocol surface (list / get / promote / create_child / link),
* returned values, not exceptions, on expected failures,
* ``external_ref`` carries the tracker-native handle so adapters don't have
  to re-encode every field in a generic shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


# ── Canonical level vocabulary ────────────────────────────────────────────────


class HierarchyLevel(str, Enum):
    """The three stable levels whilly's pipelines understand.

    ``str`` Enum so the values survive JSON serialisation / CLI without
    manual conversion.
    """

    EPIC = "epic"
    STORY = "story"
    TASK = "task"

    @property
    def child(self) -> "HierarchyLevel | None":
        """Return the level one step down, or ``None`` when at the leaf."""
        return {
            HierarchyLevel.EPIC: HierarchyLevel.STORY,
            HierarchyLevel.STORY: HierarchyLevel.TASK,
            HierarchyLevel.TASK: None,
        }[self]

    @property
    def parent(self) -> "HierarchyLevel | None":
        """Return the level one step up, or ``None`` at the root."""
        return {
            HierarchyLevel.EPIC: None,
            HierarchyLevel.STORY: HierarchyLevel.EPIC,
            HierarchyLevel.TASK: HierarchyLevel.STORY,
        }[self]


# ── WorkItem dataclass ────────────────────────────────────────────────────────


@dataclass
class WorkItem:
    """One item in the hierarchy, regardless of tracker.

    Fields:
        id: whilly-level identifier — usually the tracker-native id (e.g.
            GitHub issue URL or Jira key). Adapter-authoritative.
        level: which of Epic / Story / Task this is.
        title: short human label.
        body: long description (markdown). Empty string when unavailable.
        parent_id: id of the Level-up item, ``None`` at the root.
        children_ids: list of Level-down ids. Lazy — an adapter may return
            only direct children; grandchildren require a separate ``get()``.
        external_ref: opaque tracker-native handle (dict or typed struct)
            the adapter uses to re-locate the item without re-querying. For
            GitHub drafts: ``{"project_item_id": "..."}``. For issues:
            ``{"issue_node_id": "...", "repo": "owner/name", "number": 42}``.
            Callers should NOT parse this — it's adapter-internal plumbing.
        labels: labels/tags attached to the item (where applicable).
        status: tracker-level status string ("open"/"closed"/"todo"/…).

    The *id* + *level* pair is the stable whilly-level identity. Two items
    with the same id at different levels shouldn't happen in practice but
    the adapter is authoritative for disambiguation.
    """

    id: str
    level: HierarchyLevel
    title: str
    body: str = ""
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    external_ref: dict = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    status: str = ""

    def __post_init__(self) -> None:
        # Accept string level values (convenient for JSON round-trips).
        if isinstance(self.level, str) and not isinstance(self.level, HierarchyLevel):
            self.level = HierarchyLevel(self.level)

    # ── Small conveniences ─────────────────────────────────────────────────

    @property
    def is_root(self) -> bool:
        return self.level is HierarchyLevel.EPIC

    @property
    def is_leaf(self) -> bool:
        return self.level is HierarchyLevel.TASK


# ── Exceptions ────────────────────────────────────────────────────────────────


class HierarchyError(RuntimeError):
    """Raised by adapters on unrecoverable errors (auth, schema, network).

    Expected failures (item not found, permission denied on a single item,
    partial list results) are returned as empty collections or ``None`` from
    the Protocol methods; the caller decides how to react.
    """


# ── The Protocol ──────────────────────────────────────────────────────────────


class HierarchyAdapter(Protocol):
    """Stable contract for tracker adapters.

    Implementations must:

    * Expose ``kind`` — registry key (``"github"``, ``"jira"``, …).
    * Raise :class:`HierarchyError` on unrecoverable transport errors.
    * Return value-typed results (or ``None``) on expected failures —
      never raise for "item not found", "nothing at that level yet", etc.

    All methods accept concrete :class:`WorkItem` objects OR bare id strings
    where noted — string paths are for callers that hold only an id (CLI).
    """

    kind: str

    def get(self, id: str) -> WorkItem | None:
        """Fetch one item by id. ``None`` when not found."""
        ...

    def list_at_level(
        self,
        level: HierarchyLevel,
        *,
        parent: WorkItem | str | None = None,
        label: str | None = None,
    ) -> list[WorkItem]:
        """Return every item at *level*.

        Filters:

        - ``parent`` — only items whose parent matches. At ``EPIC`` level
          this must be ``None`` (epics have no parent).
        - ``label`` — tracker-native label/tag filter (``"whilly:ready"``).
        """
        ...

    def promote(self, item: WorkItem) -> WorkItem:
        """Promote *item* one level down (Epic draft → Story issue).

        Only meaningful for items that are NOT yet first-class at their
        level — e.g. a GitHub Project draft becoming an issue. No-op for
        items already in their target form — returns *item* unchanged.

        Raises:
            HierarchyError: promotion isn't supported for this level/kind.
        """
        ...

    def create_child(
        self,
        parent: WorkItem,
        title: str,
        body: str = "",
        *,
        labels: list[str] | None = None,
    ) -> WorkItem:
        """Create a new item one level below *parent*.

        For GitHub: parent Story (issue) → new Task (sub-issue) linked to it.
        For Jira: parent Epic → new Story under it, or Story → new Sub-task.

        The adapter handles parent-child linking atomically — the returned
        item has ``parent_id`` set and the parent's ``children_ids`` will
        reflect the new child on the next ``get()``.

        Raises:
            HierarchyError: parent level doesn't support children in this
                tracker (e.g., task → task-of-task nesting).
        """
        ...

    def link(self, parent: WorkItem, child: WorkItem) -> bool:
        """Attach an EXISTING *child* to *parent*. Use when creation already
        happened (draft→issue conversion produces an item that then needs
        to be attached to an epic).

        Returns ``True`` on success, ``False`` on any tracker-side failure
        (permission, already linked, etc.). Never raises.
        """
        ...

    def create_at_level(
        self,
        level: "HierarchyLevel",
        title: str,
        body: str = "",
    ) -> WorkItem:
        """Create a new item at the given root level (no parent).

        Use for materialising inferred Epics (from ADR-020) or creating a
        top-level Story when no Epic applies. For TASK level, adapters
        should raise :class:`HierarchyError` — tasks always need a parent
        (use :meth:`create_child` instead).

        Raises:
            HierarchyError: when the level isn't supported as a root on
                this tracker (e.g., TASK on GitHub).
        """
        ...
