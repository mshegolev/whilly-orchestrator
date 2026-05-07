"""Jira linked-artifact collector for QA release verification."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import ParseResult, unquote, urlparse

from whilly.qa_release.models import GitRepoHint, LinkedIssue, ReleaseContext, ReleaseLink
from whilly.sources.jira import JiraAuth, _flatten_adf, _jira_get, _jira_rest_path, parse_jira_key

_ISSUE_FIELDS = "summary,description,issuelinks,status,issuetype,labels,priority"
_URL_RE = re.compile(r"https?://[^\s<>()\"']+")
_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_DEPLOY_WORDS = ("deploy", "deployment", "stage", "staging", "release", "runbook", "instruction")


@dataclass(frozen=True)
class _IssuePayload:
    key: str
    payload: dict[str, Any]
    relation: str = ""


def collect_release_context(jira_ref: str, *, depth: int = 1, timeout: int = 15) -> ReleaseContext:
    """Collect Jira-linked QA release context for ``jira_ref``.

    The collector is intentionally read-only. It fetches the root Jira issue,
    optionally fetches directly linked issues, reads Jira remote links, and
    classifies discovered URLs into Confluence/GitLab/deployment/repository
    artifacts. Later pipeline stages can use this context to generate tests,
    deploy to STAGE, or create defect reports.
    """
    root_key = parse_jira_key(jira_ref)
    auth = JiraAuth.from_config()
    warnings: list[str] = []

    root_payload = _fetch_issue(auth, root_key, timeout=timeout)
    issues: list[_IssuePayload] = [_IssuePayload(root_key, root_payload, relation="root")]

    if depth > 0:
        for linked_key, relation in _linked_issue_keys(root_payload):
            try:
                issues.append(
                    _IssuePayload(linked_key, _fetch_issue(auth, linked_key, timeout=timeout), relation=relation)
                )
            except Exception as exc:  # noqa: BLE001 - linked issues should not block root intake
                warnings.append(f"linked issue {linked_key}: {type(exc).__name__}: {exc}")

    linked_issues: list[LinkedIssue] = []
    links: list[ReleaseLink] = []
    for issue in issues:
        linked_issues.append(_linked_issue_from_payload(auth, issue))
        links.extend(_links_from_issue(auth, issue, timeout=timeout, warnings=warnings))

    deduped_links = _dedupe_links(links)
    repo_hints = _dedupe_repo_hints(_repo_hint_from_link(link) for link in deduped_links)
    root_issue = linked_issues[0]
    return ReleaseContext(
        root_key=root_key,
        root_summary=root_issue.summary,
        root_url=root_issue.url,
        linked_issues=tuple(linked_issues),
        links=tuple(deduped_links),
        repo_hints=tuple(repo_hints),
        warnings=tuple(warnings),
    )


def _fetch_issue(auth: JiraAuth, key: str, *, timeout: int) -> dict[str, Any]:
    payload = _jira_get(auth, _jira_rest_path(auth, f"issue/{key}?fields={_ISSUE_FIELDS}"), timeout=timeout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Jira issue {key} returned non-object payload")
    return payload


def _linked_issue_keys(payload: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_link in (payload.get("fields") or {}).get("issuelinks") or []:
        if not isinstance(raw_link, dict):
            continue
        link_type = raw_link.get("type") or {}
        candidates = (
            (raw_link.get("outwardIssue"), str(link_type.get("outward") or link_type.get("name") or "linked")),
            (raw_link.get("inwardIssue"), str(link_type.get("inward") or link_type.get("name") or "linked")),
        )
        for issue, relation in candidates:
            if not isinstance(issue, dict):
                continue
            key = str(issue.get("key") or "").strip().upper()
            if _JIRA_KEY_RE.fullmatch(key) and key not in seen:
                seen.add(key)
                out.append((key, relation))
    return out


def _linked_issue_from_payload(auth: JiraAuth, issue: _IssuePayload) -> LinkedIssue:
    fields = issue.payload.get("fields") or {}
    return LinkedIssue(
        key=issue.key,
        summary=str(fields.get("summary") or issue.key),
        url=f"{auth.server_url}/browse/{issue.key}",
        status=str((fields.get("status") or {}).get("name") or ""),
        issue_type=str((fields.get("issuetype") or {}).get("name") or ""),
        relation=issue.relation,
        description=_description_text(fields.get("description")),
    )


def _links_from_issue(
    auth: JiraAuth,
    issue: _IssuePayload,
    *,
    timeout: int,
    warnings: list[str],
) -> list[ReleaseLink]:
    links: list[ReleaseLink] = []
    fields = issue.payload.get("fields") or {}
    description = _description_text(fields.get("description"))
    for url in _urls_from_text(description):
        links.append(
            ReleaseLink(
                url=url,
                title="description link",
                kind=_classify_url(url, "description link"),
                source_issue_key=issue.key,
                relationship="description",
            )
        )

    try:
        remote_links = _jira_get(auth, _jira_rest_path(auth, f"issue/{issue.key}/remotelink"), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - remote links are useful but optional
        warnings.append(f"remote links {issue.key}: {type(exc).__name__}: {exc}")
        remote_links = []

    if isinstance(remote_links, list):
        for raw in remote_links:
            link = _remote_link_to_release_link(raw, issue.key)
            if link is not None:
                links.append(link)
    return links


def _remote_link_to_release_link(raw: Any, source_issue_key: str) -> ReleaseLink | None:
    if not isinstance(raw, dict):
        return None
    obj = raw.get("object") if isinstance(raw.get("object"), dict) else {}
    url = str(obj.get("url") or raw.get("url") or "").strip()
    if not url:
        return None
    title = str(obj.get("title") or raw.get("relationship") or "")
    relationship = str(raw.get("relationship") or "")
    return ReleaseLink(
        url=url,
        title=title,
        kind=_classify_url(url, title),
        source_issue_key=source_issue_key,
        relationship=relationship,
    )


def _description_text(raw: Any) -> str:
    if isinstance(raw, dict):
        return _flatten_adf(raw).strip()
    return str(raw or "").strip()


def _urls_from_text(text: str) -> list[str]:
    return [match.group(0).rstrip(".,;:]") for match in _URL_RE.finditer(text or "")]


def _classify_url(url: str, title: str = "") -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    text = f"{url} {title}".lower()
    if "confluence" in text or "/wiki/" in parsed.path.lower():
        return "confluence"
    if "gitlab" in host:
        return "gitlab"
    if "github" in host:
        return "github"
    if any(word in text for word in _DEPLOY_WORDS):
        return "deployment"
    return "other"


def _repo_hint_from_link(link: ReleaseLink) -> GitRepoHint | None:
    if link.kind == "gitlab":
        return _gitlab_hint(link)
    if link.kind == "github":
        return _github_hint(link)
    parsed = urlparse(link.url)
    if _looks_like_gitlab_url(parsed):
        return _gitlab_hint(link)
    return None


def _looks_like_gitlab_url(parsed_url: ParseResult) -> bool:
    """Recognise self-hosted GitLab URLs such as ``git.company.com/group/repo/-/tree/main``."""

    host = parsed_url.netloc.lower()
    parts = [unquote(part) for part in parsed_url.path.strip("/").split("/") if part]
    return ("gitlab" in host or host.startswith("git.") or ".git." in host) and "-" in parts


def _gitlab_hint(link: ReleaseLink) -> GitRepoHint | None:
    parsed = urlparse(link.url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if not parts:
        return None
    marker = parts.index("-") if "-" in parts else len(parts)
    repo_parts = parts[:marker]
    if len(repo_parts) < 2:
        return None
    repo = "/".join(repo_parts)
    ref_type = ""
    ref = ""
    rest = parts[marker + 1 :] if marker < len(parts) else []
    if len(rest) >= 2 and rest[0] in {"tags", "tree", "commit", "blob"}:
        ref_type = {"tags": "tag", "tree": "branch", "commit": "commit", "blob": "path"}.get(rest[0], "")
        ref = "/".join(rest[1:]) if rest[0] in {"tags", "tree"} else rest[1]
    return GitRepoHint(
        provider="gitlab",
        repo_full_name=repo,
        url=link.url,
        clone_url=f"{parsed.scheme}://{parsed.netloc}/{repo}.git",
        ref=ref,
        ref_type=ref_type,
        source_issue_key=link.source_issue_key,
    )


def _github_hint(link: ReleaseLink) -> GitRepoHint | None:
    parsed = urlparse(link.url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    repo = "/".join(parts[:2])
    ref_type = ""
    ref = ""
    if len(parts) >= 4 and parts[2] in {"tree", "commit", "releases"}:
        ref_type = {"tree": "branch", "commit": "commit", "releases": "tag"}.get(parts[2], "")
        if parts[2] == "releases" and len(parts) >= 5 and parts[3] == "tag":
            ref = parts[4]
        else:
            ref = parts[3]
    return GitRepoHint(
        provider="github",
        repo_full_name=repo,
        url=link.url,
        clone_url=f"https://github.com/{repo}.git",
        ref=ref,
        ref_type=ref_type,
        source_issue_key=link.source_issue_key,
    )


def _dedupe_links(links: Iterable[ReleaseLink]) -> list[ReleaseLink]:
    seen: set[tuple[str, str]] = set()
    out: list[ReleaseLink] = []
    for link in links:
        key = (link.source_issue_key, link.url)
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out


def _dedupe_repo_hints(hints: Iterable[GitRepoHint | None]) -> list[GitRepoHint]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[GitRepoHint] = []
    for hint in hints:
        if hint is None:
            continue
        key = (hint.provider, hint.repo_full_name, hint.ref_type, hint.ref)
        if key not in seen:
            seen.add(key)
            out.append(hint)
    return out
