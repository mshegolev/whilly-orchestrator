"""Unit tests for the Jira source + JiraBoardClient.

All HTTP is stubbed via monkeypatch on the module's ``urlopen`` — tests never
touch the network. Config resolution is exercised via an in-process TOML cache
and env vars.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest

from whilly import config as cfg_mod
from whilly.jira_board import DEFAULT_JIRA_STATUS_MAPPING, JiraBoardClient, _extract_jira_key
from whilly.sources.jira import (
    JiraAuth,
    _flatten_adf,
    fetch_single_jira_issue,
    issue_to_task_dict,
    parse_jira_key,
)


# ─── parse_jira_key ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ABC-123", "ABC-123"),
        ("abc-123", "ABC-123"),  # upper-cased
        ("  ABC-123  ", "ABC-123"),
        ("https://company.atlassian.net/browse/XYZ-4567", "XYZ-4567"),
        ("https://company.atlassian.net/browse/XYZ-4567?focusedCommentId=10", "XYZ-4567"),
        ("PROJ1-42", "PROJ1-42"),
    ],
)
def test_parse_jira_key_accepts_canonical_forms(raw, expected):
    assert parse_jira_key(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "not-a-key",
        "abc/123",
        "ABC123",
        "-123",
    ],
)
def test_parse_jira_key_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_jira_key(raw)


# ─── ADF flattener ─────────────────────────────────────────────────────────────


def test_flatten_adf_basic():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello world."}]},
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "text", "text": "one"}]},
                    {"type": "listItem", "content": [{"type": "text", "text": "two"}]},
                ],
            },
        ],
    }
    text = _flatten_adf(adf)
    assert "Hello world." in text
    assert "- one" in text
    assert "- two" in text


def test_flatten_adf_handles_string_input():
    assert _flatten_adf("plain text") == "plain text"


def test_flatten_adf_handles_unknown_nodes():
    node = {"type": "mediaSingle", "content": [{"type": "text", "text": "inner"}]}
    assert "inner" in _flatten_adf(node)


# ─── issue_to_task_dict ────────────────────────────────────────────────────────


_FAKE_ISSUE = {
    "self": "https://company.atlassian.net/rest/api/3/issue/10042",
    "fields": {
        "summary": "[feature] fix login redirect",
        "description": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Steps needed"}]},
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance"}]},
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [{"type": "text", "text": "login works"}]},
                        {"type": "listItem", "content": [{"type": "text", "text": "redirect correct"}]},
                    ],
                },
            ],
        },
        "labels": ["whilly:ready", "backend"],
        "priority": {"name": "High"},
    },
}


def test_issue_to_task_dict_maps_fields():
    d = issue_to_task_dict("ABC-123", _FAKE_ISSUE)
    assert d["title"].startswith("[feature]")
    assert "Steps needed" in d["body"]
    assert d["priority"] == "high"
    assert d["jira_key"] == "ABC-123"
    assert d["url"] == "https://company.atlassian.net/browse/ABC-123"
    assert d["acceptance_criteria"] == ["login works", "redirect correct"]
    label_names = [label["name"] for label in d["labels"]]
    assert "whilly:ready" in label_names
    assert "priority:high" in label_names


def test_issue_to_task_dict_accepts_string_description():
    payload = {"fields": {"summary": "s", "description": "plain text body"}, "self": ""}
    d = issue_to_task_dict("ABC-1", payload)
    assert d["body"] == "plain text body"


# ─── fetch_single_jira_issue end-to-end (stubbed urlopen) ─────────────────────


@pytest.fixture
def _jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_SERVER_URL", "https://company.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "you@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tkn")
    monkeypatch.setattr(cfg_mod, "_toml_sections_cache", {"jira": {}})


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_fetch_single_jira_issue_writes_plan(tmp_path, monkeypatch, _jira_env):
    """End-to-end: stub the URL fetch, verify the plan has a JIRA-<key> task."""
    from whilly.sources import jira as jira_mod

    def fake_urlopen(req, timeout=None):
        assert "/rest/api/3/issue/ABC-123" in req.full_url
        return _FakeResponse(json.dumps(_FAKE_ISSUE).encode("utf-8"))

    monkeypatch.setattr(jira_mod, "urlopen", fake_urlopen)

    out = tmp_path / "plan.json"
    plan_path, stats = fetch_single_jira_issue("abc-123", out_path=out)
    assert plan_path == out.resolve()
    assert stats.new == 1

    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["tasks"]) == 1
    task = data["tasks"][0]
    assert task["id"] == "JIRA-ABC-123"
    assert task["jira_key"] == "ABC-123"
    assert task["priority"] == "high"
    assert "login redirect" in task["description"]


def test_fetch_single_jira_issue_http_error_raises(tmp_path, monkeypatch, _jira_env):
    from whilly.sources import jira as jira_mod

    from urllib.error import HTTPError

    def fake_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 404, "Not found", hdrs=None, fp=io.BytesIO(b"missing"))

    monkeypatch.setattr(jira_mod, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="404"):
        fetch_single_jira_issue("ABC-999", out_path=tmp_path / "plan.json")


def test_jira_auth_surfaces_missing_fields(monkeypatch):
    for var in ("JIRA_SERVER_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cfg_mod, "_toml_sections_cache", {"jira": {}})
    with pytest.raises(RuntimeError, match="unconfigured"):
        JiraAuth.from_config()


# ─── JiraBoardClient ───────────────────────────────────────────────────────────


def test_default_mapping_covers_all_statuses():
    required = {"pending", "in_progress", "done", "failed", "skipped", "blocked", "human_loop", "merged"}
    assert required <= set(DEFAULT_JIRA_STATUS_MAPPING.keys())


@dataclass
class _FakeTask:
    id: str
    jira_key: str | None = None


def test_extract_jira_key_from_id():
    assert _extract_jira_key(_FakeTask("JIRA-ABC-1")) == "ABC-1"


def test_extract_jira_key_honours_explicit_attr():
    assert _extract_jira_key(_FakeTask("GH-1", jira_key="XYZ-42")) == "XYZ-42"


def test_extract_jira_key_none_for_github_task():
    assert _extract_jira_key(_FakeTask("GH-5")) is None


def test_board_client_from_config_requires_enabled(monkeypatch, _jira_env):
    monkeypatch.setattr(cfg_mod, "_toml_sections_cache", {"jira": {"enabled": False}})
    assert JiraBoardClient.from_config(None) is None


def test_board_client_from_config_builds_when_configured(_jira_env):
    # _jira_env sets env but empty toml — should build since auth resolves.
    client = JiraBoardClient.from_config(None)
    assert client is not None
    assert client.auth.server_url == "https://company.atlassian.net"


def test_set_issue_status_picks_matching_transition(monkeypatch, _jira_env):
    client = JiraBoardClient(JiraAuth.from_config())
    calls: list[tuple[str, str]] = []

    def fake_api(method, path, payload=None, timeout=15, expect_empty=False):
        calls.append((method, path))
        if path.endswith("/transitions") and method == "GET":
            return {
                "transitions": [
                    {"id": "11", "to": {"name": "To Do"}},
                    {"id": "21", "to": {"name": "In Progress"}},
                    {"id": "31", "to": {"name": "Done"}},
                ]
            }
        return {}

    monkeypatch.setattr(client, "_api", fake_api)
    assert client.set_issue_status("ABC-1", "In Progress") is True
    # Should have fetched, then POSTed the transition.
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"


def test_set_issue_status_soft_fails_on_unknown_transition(monkeypatch, _jira_env):
    client = JiraBoardClient(JiraAuth.from_config())

    def fake_api(method, path, **kwargs):
        return {"transitions": [{"id": "11", "to": {"name": "To Do"}}]}

    monkeypatch.setattr(client, "_api", fake_api)
    # "Done" isn't offered → returns False without raising.
    assert client.set_issue_status("ABC-1", "Done") is False


def test_set_task_status_maps_whilly_status(monkeypatch, _jira_env):
    client = JiraBoardClient(JiraAuth.from_config())
    captured: dict = {}

    def fake_set_issue_status(key, status_name):
        captured["key"] = key
        captured["name"] = status_name
        return True

    monkeypatch.setattr(client, "set_issue_status", fake_set_issue_status)
    # A whilly "in_progress" task → Jira's default "In Progress" column name.
    assert client.set_task_status(_FakeTask("JIRA-XYZ-7"), "in_progress") is True
    assert captured["key"] == "XYZ-7"
    assert captured["name"] == "In Progress"


def test_set_task_status_ignores_non_jira_task(monkeypatch, _jira_env):
    client = JiraBoardClient(JiraAuth.from_config())
    called = []
    monkeypatch.setattr(client, "set_issue_status", lambda *a: called.append(a) or True)
    assert client.set_task_status(_FakeTask("GH-42"), "in_progress") is False
    assert called == []


def test_status_mapping_override(monkeypatch, _jira_env):
    client = JiraBoardClient(JiraAuth.from_config(), status_mapping={"in_progress": "Doing"})
    assert client.status_mapping["in_progress"] == "Doing"
    assert client.status_mapping["done"] == DEFAULT_JIRA_STATUS_MAPPING["done"]
