"""Tests for :class:`whilly.workflow.github.GitHubProjectBoard`.

No network: every ``gh api graphql`` invocation is monkey-patched to a fake
that serves canned responses. Focus is on (a) URL parsing, (b) GraphQL
payload shape, and (c) graceful-degradation contracts.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from whilly.workflow.base import BoardStatus
from whilly.workflow.github import (
    GitHubProjectBoard,
    _parse_issue_ref,
    parse_project_url,
)


# ── parse_project_url ─────────────────────────────────────────────────────────


class TestParseProjectURL:
    def test_user_level(self):
        ref = parse_project_url("https://github.com/users/mshegolev/projects/4")
        assert ref.owner_type == "user"
        assert ref.owner == "mshegolev"
        assert ref.number == 4
        assert ref.repo is None

    def test_org_level(self):
        ref = parse_project_url("https://github.com/orgs/acme/projects/12")
        assert ref.owner_type == "organization"
        assert ref.owner == "acme"
        assert ref.number == 12

    def test_repo_level(self):
        ref = parse_project_url("https://github.com/acme/backend/projects/7")
        assert ref.owner_type == "repository"
        assert ref.owner == "acme"
        assert ref.repo == "backend"
        assert ref.number == 7

    def test_trailing_slash_ok(self):
        ref = parse_project_url("https://github.com/users/mshegolev/projects/4/")
        assert ref.number == 4

    def test_unrecognised_raises(self):
        with pytest.raises(ValueError, match="unrecognised"):
            parse_project_url("https://example.com/foo")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_project_url("")


# ── _parse_issue_ref ──────────────────────────────────────────────────────────


class TestParseIssueRef:
    def test_full_url(self):
        n, repo = _parse_issue_ref("https://github.com/acme/backend/issues/42")
        assert n == 42
        assert repo == "acme/backend"

    def test_owner_repo_hash(self):
        n, repo = _parse_issue_ref("acme/backend#42")
        assert n == 42
        assert repo == "acme/backend"

    def test_bare_number(self):
        n, repo = _parse_issue_ref("42")
        assert n == 42
        assert repo is None

    def test_hash_number(self):
        n, repo = _parse_issue_ref("#42")
        assert n == 42
        assert repo is None

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="unrecognised"):
            _parse_issue_ref("not a ref")


# ── Adapter construction + gh_bin resolution ─────────────────────────────────


class TestAdapterBasics:
    def test_kind_and_url_attached(self):
        b = GitHubProjectBoard(url="https://github.com/users/x/projects/1", gh_bin="/usr/bin/gh")
        assert b.kind == "github_project"
        assert b.url == "https://github.com/users/x/projects/1"
        assert b.ref.number == 1

    def test_missing_gh_raises_on_first_call(self):
        b = GitHubProjectBoard(url="https://github.com/users/x/projects/1")
        with patch("whilly.workflow.github.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="'gh' CLI not found"):
                b.list_statuses()


# ── GraphQL mocking helpers ──────────────────────────────────────────────────


def _fake_run(responses):
    """Build a fake ``subprocess.run`` that returns canned JSON per call.

    *responses* is a list of dicts — each call consumes the next one. The
    fake is strict: running out of responses raises so tests fail loudly.
    """
    calls = []
    iterator = iter(responses)

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        try:
            payload = next(iterator)
        except StopIteration:
            raise AssertionError(f"unexpected extra subprocess call: {cmd}")
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(payload)
        result.stderr = ""
        return result

    return run, calls


# ── list_statuses ────────────────────────────────────────────────────────────


class TestListStatuses:
    def test_user_project_happy_path(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/mshegolev/projects/4",
            gh_bin="/usr/bin/gh",
        )
        fake_run, calls = _fake_run(
            [
                {
                    "data": {
                        "user": {
                            "projectV2": {
                                "id": "PVT_xxx",
                                "title": "Backlog",
                                "fields": {
                                    "nodes": [
                                        None,
                                        {
                                            "id": "PVTSSF_status",
                                            "name": "Status",
                                            "options": [
                                                {"id": "opt1", "name": "Todo"},
                                                {"id": "opt2", "name": "In Progress"},
                                                {"id": "opt3", "name": "Done"},
                                            ],
                                        },
                                    ]
                                },
                            }
                        }
                    }
                }
            ]
        )
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            statuses = board.list_statuses()
        assert [s.name for s in statuses] == ["Todo", "In Progress", "Done"]
        assert all(s.id.startswith("opt") for s in statuses)
        # One call: project info
        assert len(calls) == 1

    def test_cached_on_second_call(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/mshegolev/projects/4",
            gh_bin="/usr/bin/gh",
        )
        fake_run, calls = _fake_run(
            [
                {
                    "data": {
                        "user": {
                            "projectV2": {
                                "id": "PVT_xxx",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "SF",
                                            "name": "Status",
                                            "options": [{"id": "o", "name": "Done"}],
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            ]
        )
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            board.list_statuses()
            board.list_statuses()
        assert len(calls) == 1  # second call served from cache

    def test_no_status_field_raises(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/x/projects/1",
            gh_bin="/usr/bin/gh",
        )
        fake_run, _calls = _fake_run(
            [
                {
                    "data": {
                        "user": {
                            "projectV2": {
                                "id": "PVT_xxx",
                                "fields": {
                                    "nodes": [
                                        {"id": "SF", "name": "Priority", "options": [{"id": "o", "name": "High"}]},
                                    ]
                                },
                            }
                        }
                    }
                }
            ]
        )
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="no 'Status'"):
                board.list_statuses()

    def test_project_not_accessible_raises(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/x/projects/99",
            gh_bin="/usr/bin/gh",
        )
        fake_run, _calls = _fake_run([{"data": {"user": {"projectV2": None}}}])
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="not found or not accessible"):
                board.list_statuses()

    def test_graphql_errors_surface(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/x/projects/1",
            gh_bin="/usr/bin/gh",
        )
        fake_run, _calls = _fake_run([{"errors": [{"message": "Not Found"}]}])
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="Not Found"):
                board.list_statuses()


# ── move_item ────────────────────────────────────────────────────────────────


class TestMoveItem:
    def _project_info(self):
        return {
            "data": {
                "user": {
                    "projectV2": {
                        "id": "PVT_xxx",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "SF_status",
                                    "name": "Status",
                                    "options": [
                                        {"id": "opt_done", "name": "Done"},
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
        }

    def _items_with_issue(self, issue_number, item_id, repo="mshegolev/whilly-orchestrator"):
        return {
            "data": {
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": item_id,
                                "content": {
                                    "number": issue_number,
                                    "url": f"https://github.com/{repo}/issues/{issue_number}",
                                    "repository": {"nameWithOwner": repo},
                                },
                            }
                        ]
                    }
                }
            }
        }

    def test_happy_path(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/mshegolev/projects/4",
            gh_bin="/usr/bin/gh",
        )
        done = BoardStatus(id="opt_done", name="Done")
        fake_run, calls = _fake_run(
            [
                self._project_info(),
                self._items_with_issue(42, "PVTI_yyy"),
                {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_yyy"}}}},
            ]
        )
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            ok = board.move_item("mshegolev/whilly-orchestrator#42", done)
        assert ok is True
        assert len(calls) == 3

    def test_item_not_found_returns_false(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/mshegolev/projects/4",
            gh_bin="/usr/bin/gh",
        )
        fake_run, _calls = _fake_run(
            [
                self._project_info(),
                {"data": {"node": {"items": {"nodes": []}}}},
            ]
        )
        with patch("whilly.workflow.github.subprocess.run", side_effect=fake_run):
            ok = board.move_item("mshegolev/whilly-orchestrator#999", BoardStatus("opt_done", "Done"))
        assert ok is False

    def test_transport_error_returns_false(self):
        board = GitHubProjectBoard(
            url="https://github.com/users/mshegolev/projects/4",
            gh_bin="/usr/bin/gh",
        )

        def boom(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "network down"
            return result

        with patch("whilly.workflow.github.subprocess.run", side_effect=boom):
            ok = board.move_item("x/y#1", BoardStatus("o", "Done"))
        assert ok is False
