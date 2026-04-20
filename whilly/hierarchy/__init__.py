"""Three-level work hierarchy — tracker-agnostic Epic/Story/Task.

Public surface::

    from whilly.hierarchy import (
        HierarchyLevel,            # enum: EPIC | STORY | TASK
        WorkItem,                  # dataclass
        HierarchyAdapter,          # Protocol
        GitHubHierarchyAdapter,    # concrete impl
        HierarchyError,
        get_adapter,
        available_adapters,
    )

    adapter = get_adapter(
        "github",
        project_url="https://github.com/users/me/projects/4",
        repo="me/whilly-orchestrator",
    )
    epics = adapter.list_at_level(HierarchyLevel.EPIC)
    story = adapter.promote(epics[0])                    # Epic → Story
    task  = adapter.create_child(story, title="Do X")    # Story → Task

Design mirrors :mod:`whilly.agents`, :mod:`whilly.workflow`, :mod:`whilly.quality`:
narrow Protocol + registry + factory. New trackers drop in as sibling modules
without touching callers — see :doc:`ADR-017` for the rationale.
"""

from __future__ import annotations

from typing import Any

from whilly.hierarchy.base import (
    HierarchyAdapter,
    HierarchyError,
    HierarchyLevel,
    WorkItem,
)
from whilly.hierarchy.github import GitHubHierarchyAdapter

__all__ = [
    "HierarchyAdapter",
    "HierarchyError",
    "HierarchyLevel",
    "WorkItem",
    "GitHubHierarchyAdapter",
    "available_adapters",
    "get_adapter",
]


_REGISTRY: dict[str, type[HierarchyAdapter]] = {
    "github": GitHubHierarchyAdapter,
}


def available_adapters() -> list[str]:
    """Names of hierarchy adapters registered in this build."""
    return sorted(_REGISTRY.keys())


def get_adapter(name: str, **kwargs: Any) -> HierarchyAdapter:
    """Resolve an adapter by name.

    Args:
        name: registry key (``"github"``; ``"jira"``/``"linear"`` in future).
        kwargs: forwarded to the adapter's constructor — required keys
            are adapter-specific (GitHub needs ``project_url`` + ``repo``).

    Raises:
        ValueError: when *name* isn't registered.
    """
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown hierarchy adapter {name!r}. Available: {', '.join(available_adapters())}")
    return _REGISTRY[key](**kwargs)
