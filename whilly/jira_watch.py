"""One-shot Jira refresh snapshots for operator-driven watch workflows."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from whilly.jira_work import (
    build_jira_work_metadata,
    jira_context_hashes,
    parse_whilly_comment_command,
    release_context_repo_targets,
)
from whilly.qa_release.collector import collect_release_context
from whilly.qa_release.models import ReleaseContext
from whilly.sources.jira import JiraAuth, _flatten_adf, _jira_get, _jira_rest_path, parse_jira_key

_JIRA_WATCH_FIELDS = "summary,description,issuetype,labels,priority,status,issuelinks"
_URL_RE = re.compile(r"https?://[^\s<>()\"']+")


class JiraWorkStateRepo(Protocol):
    """Repository surface needed to persist one Jira refresh cycle."""

    async def upsert_jira_work_session(self, **kwargs: Any) -> dict[str, Any]: ...

    async def append_jira_work_event(self, **kwargs: Any) -> int: ...


@dataclass(frozen=True)
class JiraWorkSnapshot:
    """Normalized one-cycle view of a Jira issue, comments, changelog, and links."""

    issue_key: str
    summary: str
    description: str
    comments: tuple[dict[str, Any], ...]
    changelog_ids: tuple[str, ...]
    links: tuple[dict[str, Any], ...]
    repo_targets: tuple[dict[str, str], ...]
    context_hashes: dict[str, Any]
    classification: dict[str, Any]
    comment_commands: tuple[dict[str, str], ...] = ()
    last_seen_comment_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["comments"] = list(self.comments)
        data["changelog_ids"] = list(self.changelog_ids)
        data["links"] = list(self.links)
        data["repo_targets"] = list(self.repo_targets)
        data["comment_commands"] = list(self.comment_commands)
        return data


def collect_jira_work_snapshot(jira_ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
    """Fetch Jira issue, comments, changelog, remote links, and linked repo hints once."""

    key = parse_jira_key(jira_ref)
    auth = JiraAuth.from_config()
    issue_payload = _jira_get(
        auth,
        _jira_rest_path(auth, f"issue/{key}?fields={_JIRA_WATCH_FIELDS}&expand=changelog"),
        timeout=timeout,
    )
    comments_payload = _jira_get(auth, _jira_rest_path(auth, f"issue/{key}/comment"), timeout=timeout)
    release_context = collect_release_context(key, depth=1, timeout=timeout)
    return jira_work_snapshot_from_payloads(
        key,
        issue_payload=issue_payload,
        comments_payload=comments_payload,
        release_context=release_context,
    )


def jira_work_snapshot_from_payloads(
    issue_key: str,
    *,
    issue_payload: Mapping[str, Any],
    comments_payload: Mapping[str, Any],
    release_context: ReleaseContext,
) -> JiraWorkSnapshot:
    """Build a normalized snapshot from already-fetched Jira payloads."""

    fields = issue_payload.get("fields") if isinstance(issue_payload.get("fields"), Mapping) else {}
    summary = str(fields.get("summary") or issue_key)
    description = _description_text(fields.get("description"))
    comments = tuple(_comment_from_payload(item) for item in _comment_items(comments_payload))
    changelog_ids = tuple(_changelog_ids(issue_payload))
    link_dicts = tuple(link.to_dict() for link in release_context.links)
    link_urls = [str(link.get("url") or "") for link in link_dicts]
    for comment in comments:
        link_urls.extend(_urls_from_text(str(comment.get("body") or "")))
    plan_like_issue = {
        "key": issue_key,
        "summary": summary,
        "description": description,
        "fields": fields,
    }
    metadata = build_jira_work_metadata(
        plan_like_issue,
        issue_key=issue_key,
        links=link_urls,
        release_context=release_context,
    )
    commands = tuple(
        command.to_dict()
        for comment in comments
        if (command := parse_whilly_comment_command(str(comment.get("body") or ""))) is not None
    )
    return JiraWorkSnapshot(
        issue_key=issue_key,
        summary=summary,
        description=description,
        comments=comments,
        changelog_ids=changelog_ids,
        links=link_dicts,
        repo_targets=tuple(release_context_repo_targets(release_context)),
        context_hashes=jira_context_hashes(plan_like_issue, link_urls),
        classification=metadata["classification"],
        comment_commands=commands,
        last_seen_comment_id=_last_seen_comment_id(comments),
    )


async def persist_jira_work_snapshot(
    repo: JiraWorkStateRepo,
    snapshot: JiraWorkSnapshot,
    *,
    plan_id: str = "",
    state: str = "refreshed",
    readiness_verdict: str = "",
) -> dict[str, Any]:
    """Persist one Jira snapshot and append a refresh event."""

    session = await repo.upsert_jira_work_session(
        issue_key=snapshot.issue_key,
        plan_id=plan_id,
        state=state,
        work_kind=str(snapshot.classification.get("kind") or ""),
        urgency=str(snapshot.classification.get("urgency") or "normal"),
        readiness_verdict=readiness_verdict,
        summary_hash=str(snapshot.context_hashes.get("summary_hash") or ""),
        description_hash=str(snapshot.context_hashes.get("description_hash") or ""),
        link_set_hash=str(snapshot.context_hashes.get("link_set_hash") or ""),
        last_seen_comment_id=snapshot.last_seen_comment_id,
        raw_snapshot=snapshot.to_dict(),
    )
    await repo.append_jira_work_event(
        issue_key=snapshot.issue_key,
        event_type="jira.refreshed",
        payload={
            "plan_id": plan_id,
            "state": state,
            "comment_count": len(snapshot.comments),
            "changelog_count": len(snapshot.changelog_ids),
            "link_count": len(snapshot.links),
            "repo_target_count": len(snapshot.repo_targets),
            "context_hashes": snapshot.context_hashes,
        },
    )
    return session


def _description_text(raw: Any) -> str:
    if isinstance(raw, Mapping):
        return _flatten_adf(raw).strip()
    return str(raw or "").strip()


def _comment_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = payload.get("comments")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _comment_from_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    author = raw.get("author")
    author_name = ""
    if isinstance(author, Mapping):
        author_name = str(author.get("displayName") or author.get("name") or author.get("accountId") or "")
    return {
        "id": str(raw.get("id") or ""),
        "body": _description_text(raw.get("body")),
        "author": author_name,
        "created": str(raw.get("created") or ""),
        "updated": str(raw.get("updated") or ""),
    }


def _changelog_ids(issue_payload: Mapping[str, Any]) -> list[str]:
    changelog = issue_payload.get("changelog")
    histories = changelog.get("histories") if isinstance(changelog, Mapping) else []
    if not isinstance(histories, list):
        return []
    return [str(item.get("id") or "") for item in histories if isinstance(item, Mapping) and item.get("id")]


def _last_seen_comment_id(comments: tuple[dict[str, Any], ...]) -> str:
    ids = [str(comment.get("id") or "") for comment in comments if str(comment.get("id") or "")]
    if not ids:
        return ""
    return sorted(ids, key=lambda value: int(value) if value.isdigit() else value)[-1]


def _urls_from_text(text: str) -> list[str]:
    return [match.group(0).rstrip(".,;:]") for match in _URL_RE.finditer(text or "")]


__all__ = [
    "JiraWorkSnapshot",
    "collect_jira_work_snapshot",
    "jira_work_snapshot_from_payloads",
    "persist_jira_work_snapshot",
]
