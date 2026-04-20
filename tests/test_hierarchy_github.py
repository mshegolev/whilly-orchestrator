"""Tests for :class:`whilly.hierarchy.github.GitHubHierarchyAdapter`.

Network is fully mocked: every ``gh api graphql`` call is served from a
canned response list. Tests focus on:

* URL parsing (user / org projects)
* Issue ref parsing (URL / owner/repo#N / bare N)
* list_at_level — drafts for Epic, issues for Story, sub-tasks with
  both API and checkbox-fallback paths
* promote — draft → issue
* create_child — create + link with API, create + checkbox fallback
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from whilly.hierarchy.base import HierarchyError, HierarchyLevel, WorkItem
from whilly.hierarchy.github import (
    GitHubHierarchyAdapter,
    _parse_issue_ref,
    _parse_project_url,
)


# ── URL / ref parsing ────────────────────────────────────────────────────────


class TestParsing:
    def test_parse_user_project(self):
        kind, owner, number = _parse_project_url("https://github.com/users/me/projects/4")
        assert (kind, owner, number) == ("user", "me", 4)

    def test_parse_org_project(self):
        kind, owner, number = _parse_project_url("https://github.com/orgs/acme/projects/12")
        assert (kind, owner, number) == ("organization", "acme", 12)

    def test_parse_empty_raises(self):
        with pytest.raises(HierarchyError, match="empty"):
            _parse_project_url("")

    def test_parse_unrecognised_raises(self):
        with pytest.raises(HierarchyError, match="unrecognised"):
            _parse_project_url("https://example.com/not-a-project")

    def test_issue_ref_full_url(self):
        n, repo = _parse_issue_ref("https://github.com/acme/api/issues/42", default_repo="fallback/x")
        assert (n, repo) == (42, "acme/api")

    def test_issue_ref_short(self):
        n, repo = _parse_issue_ref("acme/api#42", default_repo="fallback/x")
        assert (n, repo) == (42, "acme/api")

    def test_issue_ref_bare_uses_default(self):
        n, repo = _parse_issue_ref("42", default_repo="acme/api")
        assert (n, repo) == (42, "acme/api")


# ── Shared fake GraphQL transport ────────────────────────────────────────────


def _fake_run(responses):
    """Build a subprocess.run stub that serves canned JSON per call.

    Running out of responses raises AssertionError so tests fail loudly
    on unexpected extra calls.
    """
    calls = []
    iterator = iter(responses)

    def run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        try:
            payload = next(iterator)
        except StopIteration:
            raise AssertionError(f"unexpected extra subprocess call: {cmd}")
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(payload) if isinstance(payload, dict) else payload
        result.stderr = ""
        return result

    return run, calls


def _project_and_repo_payload(project_id="PVT_x", repo_id="R_y"):
    return {
        "data": {
            "user": {
                "projectV2": {"id": project_id, "title": "Backlog"},
                "repository": {"id": repo_id, "nameWithOwner": "mshegolev/whilly-orchestrator"},
            }
        }
    }


def _draft(title, body="", item_id="PVTI_d1", draft_id="DI_1"):
    return {
        "id": item_id,
        "content": {"__typename": "DraftIssue", "id": draft_id, "title": title, "body": body},
    }


def _issue_item(number, title, item_id, issue_node_id, url="https://x", repo="m/r"):
    return {
        "id": item_id,
        "content": {
            "__typename": "Issue",
            "id": issue_node_id,
            "number": number,
            "title": title,
            "body": "",
            "url": url,
            "state": "OPEN",
            "labels": {"nodes": []},
            "repository": {"nameWithOwner": repo},
        },
    }


# ── Constructor + transport basics ───────────────────────────────────────────


class TestConstruction:
    def test_kind_and_fields(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/x",
            gh_bin="/usr/bin/gh",
        )
        assert a.kind == "github"
        assert a.project_url.endswith("/projects/4")
        assert a.repo == "me/x"

    def test_bad_repo_raises(self):
        with pytest.raises(HierarchyError, match="owner/repo"):
            GitHubHierarchyAdapter(
                project_url="https://github.com/users/me/projects/4",
                repo="not-a-slug",
            )


# ── list_at_level(EPIC) — drafts ─────────────────────────────────────────────


class TestListEpics:
    def _adapter(self):
        return GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/x",
            gh_bin="/usr/bin/gh",
        )

    def test_returns_drafts_only(self):
        a = self._adapter()
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "node": {
                            "items": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    _draft("Rate-limit API", "kpi: p95 < 1s", item_id="PVTI_a"),
                                    _issue_item(7, "Existing", "PVTI_b", "I_b"),  # should be skipped
                                    _draft("Docs overhaul", item_id="PVTI_c"),
                                ],
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            epics = a.list_at_level(HierarchyLevel.EPIC)
        assert [e.title for e in epics] == ["Rate-limit API", "Docs overhaul"]
        assert all(e.level is HierarchyLevel.EPIC for e in epics)
        assert epics[0].external_ref["project_item_id"] == "PVTI_a"
        assert epics[0].body == "kpi: p95 < 1s"

    def test_label_best_effort_filters_by_title_body(self):
        a = self._adapter()
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "node": {
                            "items": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    _draft("Rate-limit API", item_id="PVTI_a"),
                                    _draft("MVP landing page", body="whilly:ready", item_id="PVTI_c"),
                                ],
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            epics = a.list_at_level(HierarchyLevel.EPIC, label="whilly:ready")
        assert [e.title for e in epics] == ["MVP landing page"]


# ── list_at_level(STORY) — issues ────────────────────────────────────────────


class TestListStories:
    def test_issues_mapped_to_workitems(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "repository": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "I_1",
                                        "number": 10,
                                        "title": "Login bug",
                                        "body": "Steps: ...",
                                        "url": "https://github.com/me/api/issues/10",
                                        "state": "OPEN",
                                        "labels": {"nodes": [{"name": "whilly:ready"}]},
                                        "repository": {"nameWithOwner": "me/api"},
                                    }
                                ]
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            stories = a.list_at_level(HierarchyLevel.STORY, label="whilly:ready")
        assert len(stories) == 1
        s = stories[0]
        assert s.level is HierarchyLevel.STORY
        assert s.title == "Login bug"
        assert s.status == "open"
        assert "whilly:ready" in s.labels
        assert s.external_ref["number"] == 10
        assert s.external_ref["repo"] == "me/api"


# ── list_at_level(TASK) — checkbox parsing fallback ──────────────────────────


class TestListTasks:
    def test_parses_checkbox_references(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )
        parent = WorkItem(
            id="me/api#10",
            level=HierarchyLevel.STORY,
            title="parent",
            body="Plan:\n- [ ] #11\n- [x] #12\nOther text",
        )
        # Each checkbox number → one _Q_ISSUE_BY_NUMBER call.
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "I_11",
                                "number": 11,
                                "title": "Sub A",
                                "body": "",
                                "url": "https://github.com/me/api/issues/11",
                                "state": "OPEN",
                                "labels": {"nodes": []},
                                "repository": {"nameWithOwner": "me/api"},
                            }
                        }
                    }
                },
                {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "I_12",
                                "number": 12,
                                "title": "Sub B",
                                "body": "",
                                "url": "https://github.com/me/api/issues/12",
                                "state": "CLOSED",
                                "labels": {"nodes": []},
                                "repository": {"nameWithOwner": "me/api"},
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            tasks = a.list_at_level(HierarchyLevel.TASK, parent=parent)
        assert [t.title for t in tasks] == ["Sub A", "Sub B"]
        assert all(t.level is HierarchyLevel.TASK for t in tasks)
        assert all(t.parent_id == parent.id for t in tasks)

    def test_requires_parent(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )
        with pytest.raises(HierarchyError, match="requires parent"):
            a.list_at_level(HierarchyLevel.TASK)


# ── promote: Epic draft → Story ──────────────────────────────────────────────


class TestPromote:
    def test_draft_to_issue(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )
        draft = WorkItem(
            id="PVTI_a",
            level=HierarchyLevel.EPIC,
            title="Rate-limit API",
            body="kpi p95",
            external_ref={"project_item_id": "PVTI_a"},
        )
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "convertProjectV2DraftIssueItemToIssue": {
                            "item": {
                                "id": "PVTI_a",
                                "content": {
                                    "id": "I_new",
                                    "number": 99,
                                    "title": "Rate-limit API",
                                    "url": "https://github.com/me/api/issues/99",
                                },
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            story = a.promote(draft)
        assert story.level is HierarchyLevel.STORY
        assert story.external_ref["number"] == 99
        assert story.external_ref["issue_node_id"] == "I_new"
        assert story.body == "kpi p95"  # body carried over

    def test_promote_story_is_noop(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )
        s = WorkItem(id="x", level=HierarchyLevel.STORY, title="t")
        assert a.promote(s) is s


# ── create_child: Story → Task (API path + checkbox fallback) ───────────────


class TestCreateChild:
    def _adapter(self):
        return GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )

    def _parent(self):
        return WorkItem(
            id="https://github.com/me/api/issues/10",
            level=HierarchyLevel.STORY,
            title="Parent",
            body="",
            external_ref={
                "issue_node_id": "I_parent",
                "repo": "me/api",
                "number": 10,
                "url": "https://github.com/me/api/issues/10",
            },
        )

    def test_sub_issue_api_happy_path(self):
        a = self._adapter()
        parent = self._parent()
        fake_run, calls = _fake_run(
            [
                _project_and_repo_payload(),
                # createIssue response
                {
                    "data": {
                        "createIssue": {
                            "issue": {
                                "id": "I_child",
                                "number": 11,
                                "title": "Child task",
                                "url": "https://github.com/me/api/issues/11",
                            }
                        }
                    }
                },
                # addSubIssue response
                {"data": {"addSubIssue": {"issue": {"id": "I_parent"}, "subIssue": {"id": "I_child"}}}},
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            child = a.create_child(parent, "Child task")
        assert child.level is HierarchyLevel.TASK
        assert child.parent_id == parent.id
        assert child.external_ref["number"] == 11
        # Three GraphQL calls exactly — no checkbox fallback touched gh issue edit.
        assert len(calls) == 3

    def test_fallback_to_checkbox_when_sub_issue_api_fails(self):
        a = self._adapter()
        parent = self._parent()

        # First two calls: project-and-repo + createIssue (JSON), third is
        # addSubIssue which errors, and the fallback runs ``gh issue edit``
        # as a non-graphql subprocess. We hand-wire a side_effect instead
        # of using _fake_run for that last bit.
        project_payload = _project_and_repo_payload()
        create_payload = {
            "data": {
                "createIssue": {
                    "issue": {
                        "id": "I_child",
                        "number": 11,
                        "title": "Child task",
                        "url": "https://github.com/me/api/issues/11",
                    }
                }
            }
        }
        add_sub_error_payload = {"errors": [{"message": "addSubIssue not available"}]}

        responses = [project_payload, create_payload, add_sub_error_payload]

        def side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            # graphql calls
            if cmd[-1] == "-" and "graphql" in cmd:
                payload = responses.pop(0)
                result.returncode = 0
                result.stdout = json.dumps(payload)
                result.stderr = ""
                return result
            if "graphql" in cmd:
                payload = responses.pop(0)
                result.returncode = 0
                result.stdout = json.dumps(payload)
                result.stderr = ""
                return result
            # `gh issue edit ... --body-file -`: fallback path
            if "issue" in cmd and "edit" in cmd:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                return result
            raise AssertionError(f"unexpected cmd: {cmd}")

        with patch("whilly.hierarchy.github.subprocess.run", side_effect=side_effect):
            child = a.create_child(parent, "Child task")
        assert child.level is HierarchyLevel.TASK
        # Flag flipped on the adapter — subsequent calls won't retry the API.
        assert a._sub_issue_api_available is False

    def test_task_cannot_have_children(self):
        a = self._adapter()
        leaf = WorkItem(id="x", level=HierarchyLevel.TASK, title="t")
        with pytest.raises(HierarchyError, match="cannot have children"):
            a.create_child(leaf, "nope")


# ── link: attach existing ────────────────────────────────────────────────────


class TestCreateAtLevel:
    def _adapter(self):
        return GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )

    def test_create_epic_creates_draft(self):
        a = self._adapter()
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "addProjectV2DraftIssue": {
                            "projectItem": {
                                "id": "PVTI_new",
                                "content": {"id": "DI_new", "title": "Auth initiative", "body": "b"},
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            epic = a.create_at_level(HierarchyLevel.EPIC, "Auth initiative", "b")
        assert epic.level is HierarchyLevel.EPIC
        assert epic.title == "Auth initiative"
        assert epic.external_ref["project_item_id"] == "PVTI_new"

    def test_create_story_creates_issue(self):
        a = self._adapter()
        fake_run, _calls = _fake_run(
            [
                _project_and_repo_payload(),
                {
                    "data": {
                        "createIssue": {
                            "issue": {
                                "id": "I_s",
                                "number": 50,
                                "title": "New story",
                                "url": "https://github.com/me/api/issues/50",
                            }
                        }
                    }
                },
            ]
        )
        with patch("whilly.hierarchy.github.subprocess.run", side_effect=fake_run):
            story = a.create_at_level(HierarchyLevel.STORY, "New story", "body")
        assert story.level is HierarchyLevel.STORY
        assert story.external_ref["number"] == 50

    def test_create_task_raises(self):
        a = self._adapter()
        with pytest.raises(HierarchyError, match="tasks need a parent|create_child"):
            a.create_at_level(HierarchyLevel.TASK, "x", "y")


class TestLink:
    def test_task_as_parent_returns_false(self):
        a = GitHubHierarchyAdapter(
            project_url="https://github.com/users/me/projects/4",
            repo="me/api",
            gh_bin="/usr/bin/gh",
        )
        t = WorkItem(id="t", level=HierarchyLevel.TASK, title="t")
        c = WorkItem(id="c", level=HierarchyLevel.TASK, title="c")
        assert a.link(t, c) is False
