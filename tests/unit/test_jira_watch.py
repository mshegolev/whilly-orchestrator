"""Tests for one-shot Jira watch snapshots."""

from __future__ import annotations

import asyncio

from whilly.jira_watch import jira_work_snapshot_from_payloads, persist_jira_work_snapshot
from whilly.qa_release.models import GitRepoHint, ReleaseContext, ReleaseLink


def test_jira_work_snapshot_includes_comments_changelog_links_and_hashes() -> None:
    issue_payload = {
        "key": "ABC-123",
        "fields": {
            "summary": "Fix ETL job",
            "description": "See https://gitlab.company/platform/etl/-/tree/main",
            "issuetype": {"name": "Bug"},
            "priority": {"name": "High"},
        },
        "changelog": {"histories": [{"id": "10001"}, {"id": "10002"}]},
    }
    comments_payload = {
        "comments": [
            {
                "id": "20001",
                "body": "please run /whilly plan after checking https://gitlab.company/platform/etl/-/merge_requests/7",
                "author": {"displayName": "Dev"},
                "created": "2026-05-11T10:00:00.000+0000",
                "updated": "2026-05-11T10:01:00.000+0000",
            }
        ]
    }
    release_context = ReleaseContext(
        root_key="ABC-123",
        root_summary="Fix ETL job",
        root_url="https://jira/browse/ABC-123",
        linked_issues=(),
        links=(
            ReleaseLink(
                url="https://gitlab.company/platform/etl/-/merge_requests/7",
                kind="gitlab",
                source_issue_key="ABC-123",
            ),
        ),
        repo_hints=(
            GitRepoHint(
                provider="gitlab",
                repo_full_name="platform/etl",
                url="https://gitlab.company/platform/etl/-/merge_requests/7",
                clone_url="https://gitlab.company/platform/etl.git",
            ),
        ),
    )

    snapshot = jira_work_snapshot_from_payloads(
        "ABC-123",
        issue_payload=issue_payload,
        comments_payload=comments_payload,
        release_context=release_context,
    )

    assert snapshot.issue_key == "ABC-123"
    assert snapshot.summary == "Fix ETL job"
    assert snapshot.last_seen_comment_id == "20001"
    assert snapshot.changelog_ids == ("10001", "10002")
    assert snapshot.comment_commands[0]["action"] == "plan"
    assert snapshot.repo_targets[0]["id"] == "gitlab:platform/etl"
    assert snapshot.context_hashes["description_hash"]
    assert "https://gitlab.company/platform/etl/-/merge_requests/7" in snapshot.context_hashes["links"]


def test_persist_jira_work_snapshot_upserts_session_and_appends_event() -> None:
    snapshot = jira_work_snapshot_from_payloads(
        "ABC-123",
        issue_payload={
            "fields": {
                "summary": "Production checkout fails",
                "description": "Actual error. Expected success. Steps to reproduce. Rollback plan. Smoke test.",
                "issuetype": {"name": "Bug"},
                "priority": {"name": "Highest"},
            },
            "changelog": {"histories": [{"id": "10001"}]},
        },
        comments_payload={"comments": [{"id": "20001", "body": "/whilly run"}]},
        release_context=ReleaseContext(
            root_key="ABC-123",
            root_summary="Production checkout fails",
            root_url="https://jira/browse/ABC-123",
            linked_issues=(),
            links=(),
            repo_hints=(),
        ),
    )
    repo = _FakeRepo()

    session = asyncio.run(persist_jira_work_snapshot(repo, snapshot, plan_id="jira-abc-123"))

    assert session == {"issue_key": "ABC-123"}
    assert repo.upserts[0]["issue_key"] == "ABC-123"
    assert repo.upserts[0]["plan_id"] == "jira-abc-123"
    assert repo.upserts[0]["work_kind"] == "bug"
    assert repo.upserts[0]["urgency"] == "hotfix"
    assert repo.upserts[0]["last_seen_comment_id"] == "20001"
    assert repo.events[0]["event_type"] == "jira.refreshed"
    assert repo.events[0]["payload"]["comment_count"] == 1


class _FakeRepo:
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    async def upsert_jira_work_session(self, **kwargs: object) -> dict[str, str]:
        self.upserts.append(kwargs)
        return {"issue_key": str(kwargs["issue_key"])}

    async def append_jira_work_event(self, **kwargs: object) -> int:
        self.events.append(kwargs)
        return 1
