"""Unit tests for the Projects v2 board sync.

Tests the ``ProjectBoardClient`` mutation logic and the ``TaskManager``
``on_status_change`` callback end-to-end, using a stubbed ``_gh_api`` so no
network calls fire during CI.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from whilly import config as cfg_mod
from whilly.project_board import DEFAULT_STATUS_MAPPING, ProjectBoardClient
from whilly.task_manager import Task, TaskManager


@dataclass
class _FakeTask:
    id: str
    prd_requirement: str = ""


# ─── URL parsing ───────────────────────────────────────────────────────────────


def test_parse_user_project_url():
    c = ProjectBoardClient("https://github.com/users/alice/projects/7")
    assert c._owner == "alice"
    assert c._owner_type == "user"
    assert c._number == 7


def test_parse_org_project_url():
    c = ProjectBoardClient("https://github.com/orgs/acme/projects/3/views/2?layout=board")
    assert c._owner == "acme"
    assert c._owner_type == "organization"
    assert c._number == 3


def test_parse_rejects_repo_project_url():
    with pytest.raises(ValueError):
        ProjectBoardClient("https://github.com/alice/repo/projects/3")


# ─── Issue reference extraction ───────────────────────────────────────────────


def test_extract_issue_ref_from_task_id_only():
    n, repo = ProjectBoardClient._extract_issue_ref(_FakeTask("GH-42"))
    assert (n, repo) == (42, None)


def test_extract_issue_ref_from_task_id_with_prd_url():
    task = _FakeTask("GH-42", prd_requirement="https://github.com/alice/myrepo/issues/42")
    n, repo = ProjectBoardClient._extract_issue_ref(task)
    assert (n, repo) == (42, "alice/myrepo")


def test_extract_issue_ref_non_github_task_returns_none():
    n, repo = ProjectBoardClient._extract_issue_ref(_FakeTask("TASK-001"))
    assert (n, repo) == (None, None)


# ─── Status mapping defaults ───────────────────────────────────────────────────


def test_default_status_mapping_covers_all_whilly_statuses():
    required = {"pending", "in_progress", "done", "failed", "skipped"}
    assert required <= set(DEFAULT_STATUS_MAPPING.keys())


def test_status_mapping_is_override_merged():
    c = ProjectBoardClient(
        "https://github.com/users/alice/projects/1",
        status_mapping={"in_progress": "Doing"},
    )
    assert c.status_mapping["in_progress"] == "Doing"
    # Unoverridden keys still have defaults.
    assert c.status_mapping["done"] == DEFAULT_STATUS_MAPPING["done"]


# ─── Metadata fetch + set_issue_status (stubbed) ──────────────────────────────


_FAKE_METADATA = {
    "data": {
        "user": {
            "projectV2": {
                "id": "PVT_1",
                "fields": {
                    "nodes": [
                        {
                            "__typename": "ProjectV2SingleSelectField",
                            "id": "FLD_STATUS",
                            "name": "Status",
                            "options": [
                                {"id": "OPT_TODO", "name": "Todo"},
                                {"id": "OPT_INPROG", "name": "In Progress"},
                                {"id": "OPT_DONE", "name": "Done"},
                            ],
                        },
                        {"__typename": "ProjectV2Field", "id": "FLD_TITLE", "name": "Title"},
                    ]
                },
                "items": {
                    "nodes": [
                        {
                            "id": "ITEM_1",
                            "content": {
                                "__typename": "Issue",
                                "number": 42,
                                "repository": {"nameWithOwner": "alice/myrepo"},
                            },
                        },
                        {
                            "id": "ITEM_2",
                            "content": {
                                "__typename": "Issue",
                                "number": 7,
                                "repository": {"nameWithOwner": "alice/other"},
                            },
                        },
                    ]
                },
            }
        }
    }
}


@pytest.fixture
def stubbed_client(monkeypatch):
    """Return a ProjectBoardClient with _gh_api swapped out for a capturing fake."""
    calls: list[tuple[str, dict]] = []

    def fake_gh_api(query: str, **variables):
        calls.append((query, variables))
        if "updateProjectV2ItemFieldValue" in query:
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["item"]}}}}
        return _FAKE_METADATA

    monkeypatch.setattr(ProjectBoardClient, "_gh_api", staticmethod(fake_gh_api))
    client = ProjectBoardClient("https://github.com/users/alice/projects/1")
    return client, calls


def test_set_issue_status_success(stubbed_client):
    client, calls = stubbed_client
    ok = client.set_issue_status(42, "alice/myrepo", "In Progress")
    assert ok is True
    # One metadata query + one mutation.
    assert len(calls) == 2
    mutation_args = calls[1][1]
    assert mutation_args["item"] == "ITEM_1"
    assert mutation_args["option"] == "OPT_INPROG"
    assert mutation_args["project"] == "PVT_1"


def test_set_issue_status_unknown_status(stubbed_client):
    client, _ = stubbed_client
    assert client.set_issue_status(42, "alice/myrepo", "Mystery") is False


def test_set_issue_status_missing_card(stubbed_client):
    client, _ = stubbed_client
    assert client.set_issue_status(999, "alice/myrepo", "Todo") is False


def test_set_issue_status_reuses_cached_metadata(stubbed_client):
    client, calls = stubbed_client
    client.set_issue_status(42, "alice/myrepo", "Todo")
    client.set_issue_status(7, "alice/other", "Done")
    # Metadata should be fetched exactly once — two mutations after that.
    metadata_calls = [c for c in calls if "updateProjectV2ItemFieldValue" not in c[0]]
    assert len(metadata_calls) == 1
    mutations = [c for c in calls if "updateProjectV2ItemFieldValue" in c[0]]
    assert len(mutations) == 2


def test_set_task_status_maps_whilly_to_board_column(stubbed_client):
    client, calls = stubbed_client
    client.default_repo = "alice/myrepo"
    task = _FakeTask("GH-42")
    assert client.set_task_status(task, "in_progress") is True
    mutation_args = calls[-1][1]
    assert mutation_args["option"] == "OPT_INPROG"


def test_set_task_status_ignores_non_github_tasks(stubbed_client):
    client, calls = stubbed_client
    assert client.set_task_status(_FakeTask("TASK-X"), "in_progress") is False
    # No mutation fired (only maybe metadata, but even that was skipped on early return).
    assert not any("updateProjectV2ItemFieldValue" in c[0] for c in calls)


# ─── TaskManager.on_status_change plumbing ────────────────────────────────────


def test_mark_status_invokes_callback_only_on_actual_transition(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"project":"t","tasks":[{"id":"A","phase":"p","category":"x","priority":"medium","status":"pending","description":"x"}]}',
        encoding="utf-8",
    )
    tm = TaskManager(plan)
    seen: list[tuple[str, str, str]] = []
    tm.on_status_change = lambda task, old, new: seen.append((task.id, old, new))

    tm.mark_status(["A"], "in_progress")
    tm.mark_status(["A"], "in_progress")  # no-op — same status
    tm.mark_status(["A"], "done")

    assert seen == [("A", "pending", "in_progress"), ("A", "in_progress", "done")]


def test_mark_status_callback_exception_does_not_break_save(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"project":"t","tasks":[{"id":"A","phase":"p","category":"x","priority":"medium","status":"pending","description":"x"}]}',
        encoding="utf-8",
    )
    tm = TaskManager(plan)
    tm.on_status_change = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))

    tm.mark_status(["A"], "in_progress")
    # Reload from disk — status change persisted even though callback blew up.
    tm.reload()
    assert tm.tasks[0].status == "in_progress"


def test_from_config_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(cfg_mod, "_toml_sections_cache", {"project_board": {}})
    assert ProjectBoardClient.from_config(None) is None


def test_from_config_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(
        cfg_mod,
        "_toml_sections_cache",
        {"project_board": {"url": "https://github.com/users/alice/projects/1", "enabled": False}},
    )
    assert ProjectBoardClient.from_config(None) is None


def test_from_config_builds_client(monkeypatch):
    monkeypatch.setattr(
        cfg_mod,
        "_toml_sections_cache",
        {
            "project_board": {
                "url": "https://github.com/users/alice/projects/1",
                "enabled": True,
                "default_repo": "alice/myrepo",
                "status_mapping": {"in_progress": "Doing"},
            }
        },
    )
    client = ProjectBoardClient.from_config(None)
    assert client is not None
    assert client.default_repo == "alice/myrepo"
    assert client.status_mapping["in_progress"] == "Doing"


# ─── Real Task integration ─────────────────────────────────────────────────────


def test_real_task_object_has_derivable_issue_number():
    task = Task(
        id="GH-42",
        phase="GH-Issues",
        category="github-issue",
        priority="medium",
        description="Some title\n\nSome body",
        status="pending",
        prd_requirement="https://github.com/alice/myrepo/issues/42",
    )
    n, repo = ProjectBoardClient._extract_issue_ref(task)
    assert (n, repo) == (42, "alice/myrepo")
