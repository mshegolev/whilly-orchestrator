"""Tests for :mod:`whilly.hierarchy` foundation — enum, dataclass, Protocol,
registry. Adapter-specific behaviour lives in ``test_hierarchy_github.py``.
"""

from __future__ import annotations

import pytest

from whilly.hierarchy import (
    GitHubHierarchyAdapter,
    HierarchyAdapter,
    HierarchyError,
    HierarchyLevel,
    WorkItem,
    available_adapters,
    get_adapter,
)


class TestHierarchyLevel:
    def test_three_levels(self):
        assert {e.value for e in HierarchyLevel} == {"epic", "story", "task"}

    def test_child_walks_down(self):
        assert HierarchyLevel.EPIC.child is HierarchyLevel.STORY
        assert HierarchyLevel.STORY.child is HierarchyLevel.TASK
        assert HierarchyLevel.TASK.child is None

    def test_parent_walks_up(self):
        assert HierarchyLevel.EPIC.parent is None
        assert HierarchyLevel.STORY.parent is HierarchyLevel.EPIC
        assert HierarchyLevel.TASK.parent is HierarchyLevel.STORY

    def test_str_enum_round_trip(self):
        assert HierarchyLevel("epic") is HierarchyLevel.EPIC
        assert HierarchyLevel.EPIC.value == "epic"


class TestWorkItem:
    def test_defaults(self):
        w = WorkItem(id="x", level=HierarchyLevel.STORY, title="t")
        assert w.body == ""
        assert w.parent_id is None
        assert w.children_ids == []
        assert w.external_ref == {}
        assert w.labels == []
        assert w.status == ""
        assert w.is_root is False
        assert w.is_leaf is False

    def test_is_root_epic(self):
        w = WorkItem(id="x", level=HierarchyLevel.EPIC, title="t")
        assert w.is_root is True

    def test_is_leaf_task(self):
        w = WorkItem(id="x", level=HierarchyLevel.TASK, title="t")
        assert w.is_leaf is True

    def test_level_string_coerces_to_enum(self):
        # JSON round-trip friendliness — accept string value in the dataclass.
        w = WorkItem(id="x", level="story", title="t")  # type: ignore[arg-type]
        assert w.level is HierarchyLevel.STORY


class TestRegistry:
    def test_github_registered(self):
        assert "github" in available_adapters()

    def test_get_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown hierarchy adapter"):
            get_adapter("cobol-tracker")

    def test_get_github_requires_args(self):
        with pytest.raises(TypeError):
            get_adapter("github")  # project_url / repo missing

    def test_adapter_satisfies_protocol(self):
        a: HierarchyAdapter = GitHubHierarchyAdapter(
            project_url="https://github.com/users/x/projects/1",
            repo="x/y",
            gh_bin="/usr/bin/gh",
        )
        assert a.kind == "github"
        for attr in ("get", "list_at_level", "promote", "create_child", "link"):
            assert callable(getattr(a, attr, None)), attr


class TestHierarchyError:
    def test_is_runtime_error(self):
        assert issubclass(HierarchyError, RuntimeError)
