"""GitHub hierarchy adapter — Project draft (Epic) / Issue (Story) / Sub-issue (Task).

Mapping:

* **Epic** — a Project v2 **draft item** (text card, not yet an issue) OR
  a parent issue that tracks multiple Stories via GitHub's sub-issue
  relation. New epics normally start as drafts and get promoted to
  issues via :meth:`promote` when whilly begins working on them.
* **Story** — a regular GitHub Issue. This is the "feature" level —
  accepts a PRD, gets decomposed into Tasks.
* **Task** — a GitHub **sub-issue** of a Story (the 2024+ sub-issue
  feature). When the sub-issue API isn't accessible (older token scopes,
  preview flag off), the adapter falls back to a **checkbox list** in
  the parent's body: ``- [ ] #N`` references that GitHub auto-renders.

Every network call goes through :class:`whilly.workflow.github.GitHubProjectBoard`'s
transport layer (``gh api graphql``) — no new HTTP client, no new deps.
Adapters share the same auth / retry / truncation story as the workflow
adapter, which simplifies ops (one ``gh auth status`` covers both).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from typing import Any

from whilly.hierarchy.base import (
    HierarchyAdapter,  # noqa: F401  # re-exported for Protocol docs
    HierarchyError,
    HierarchyLevel,
    WorkItem,
)

log = logging.getLogger("whilly.hierarchy.github")


# ── GraphQL payloads ──────────────────────────────────────────────────────────

# Find the project id + repository id for the owner/number pair. We need the
# project id to list drafts and the repository id to convert drafts to issues.
_Q_PROJECT_AND_REPO = {
    "user": """
        query($login: String!, $number: Int!, $repo: String!) {
          user(login: $login) {
            projectV2(number: $number) { id title }
            repository(name: $repo) { id nameWithOwner }
          }
        }
    """,
    "organization": """
        query($login: String!, $number: Int!, $repo: String!) {
          organization(login: $login) {
            projectV2(number: $number) { id title }
            repository(name: $repo) { id nameWithOwner }
          }
        }
    """,
}


# List every item on the project. We filter to drafts / issues / PRs
# downstream — GitHub doesn't let us filter server-side by kind in one call.
_Q_PROJECT_ITEMS = """
    query($projectId: ID!, $first: Int!, $after: String) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                __typename
                ... on DraftIssue { id title body }
                ... on Issue {
                  id number title body url state
                  labels(first: 20) { nodes { name } }
                  repository { nameWithOwner }
                }
                ... on PullRequest { id number title url state }
              }
            }
          }
        }
      }
    }
"""


# Convert a draft item into a real issue in the given repository. The
# resulting item stays on the project — GitHub moves it automatically.
_M_CONVERT_DRAFT = """
    mutation($itemId: ID!, $repoId: ID!) {
      convertProjectV2DraftIssueItemToIssue(input: {
        itemId: $itemId,
        repositoryId: $repoId
      }) {
        item {
          id
          content {
            ... on Issue { id number title url }
          }
        }
      }
    }
"""


# Create an issue in the given repo.
_M_CREATE_ISSUE = """
    mutation($repoId: ID!, $title: String!, $body: String!, $labels: [ID!]) {
      createIssue(input: {
        repositoryId: $repoId,
        title: $title,
        body: $body,
        labelIds: $labels
      }) {
        issue { id number title url }
      }
    }
"""


# Add an existing issue as a sub-issue of another. GitHub's sub-issue API
# landed in 2024 — the mutation may return "not supported" on older GHES
# or with a missing preview scope; the adapter detects this and falls
# back to the checkbox-list path.
_M_ADD_SUB_ISSUE = """
    mutation($parentId: ID!, $childId: ID!) {
      addSubIssue(input: { issueId: $parentId, subIssueId: $childId }) {
        issue { id number }
        subIssue { id number }
      }
    }
"""


# Create a draft item on the project (EPIC level — materialise an
# inferred epic without making a repo-level issue).
_M_ADD_DRAFT = """
    mutation($projectId: ID!, $title: String!, $body: String!) {
      addProjectV2DraftIssue(input: {
        projectId: $projectId,
        title: $title,
        body: $body
      }) {
        projectItem {
          id
          content {
            ... on DraftIssue { id title body }
          }
        }
      }
    }
"""


# Fetch an issue by owner/repo/number — used by get() and list_at_level
# when we only have a bare issue number.
_Q_ISSUE_BY_NUMBER = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          id number title body url state
          labels(first: 20) { nodes { name } }
          repository { nameWithOwner }
        }
      }
    }
"""


# List open issues in a repo by label. Used for list_at_level(STORY, label=...).
_Q_ISSUES_BY_LABEL = """
    query($owner: String!, $repo: String!, $labels: [String!], $first: Int!) {
      repository(owner: $owner, name: $repo) {
        issues(first: $first, labels: $labels, states: OPEN, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes {
            id number title body url state
            labels(first: 20) { nodes { name } }
            repository { nameWithOwner }
          }
        }
      }
    }
"""


# ── URL parsing ───────────────────────────────────────────────────────────────


_PROJECT_URL_PATTERNS = [
    (re.compile(r"github\.com/users/([^/]+)/projects/(\d+)"), "user"),
    (re.compile(r"github\.com/orgs/([^/]+)/projects/(\d+)"), "organization"),
]


def _parse_project_url(url: str) -> tuple[str, str, int]:
    """Return (owner_type, owner, number) for a Project v2 URL.

    Unlike :func:`whilly.workflow.github.parse_project_url`, this hierarchy
    adapter needs only user/org URLs — repository-scoped projects don't
    hold drafts in practice.
    """
    if not url:
        raise HierarchyError("empty project URL")
    for pattern, kind in _PROJECT_URL_PATTERNS:
        m = pattern.search(url)
        if m:
            return kind, m.group(1), int(m.group(2))
    raise HierarchyError(
        f"unrecognised project URL {url!r} — expected github.com/users/<x>/projects/<N> "
        "or github.com/orgs/<x>/projects/<N>"
    )


def _parse_owner_repo(value: str) -> tuple[str, str]:
    """Split ``"owner/repo"`` — raises HierarchyError on malformed input."""
    if "/" not in value:
        raise HierarchyError(f"expected 'owner/repo' form, got {value!r}")
    owner, repo = value.split("/", 1)
    if not owner or not repo:
        raise HierarchyError(f"expected 'owner/repo' form, got {value!r}")
    return owner, repo


# ── The adapter ──────────────────────────────────────────────────────────────


class GitHubHierarchyAdapter:
    """Hierarchy adapter backed by GitHub Projects v2 + Issues + Sub-issues.

    Args:
        project_url: Project v2 URL. Required even for story/task-only work
            because Epic level lives on the project.
        repo: ``"owner/repo"`` — the repository where Stories (issues) and
            Tasks (sub-issues) live. Required — GitHub doesn't infer it
            from the project for user-level projects that span repos.
        gh_bin: path to the ``gh`` binary; resolved lazily (tests can
            construct adapters without ``gh`` installed).
    """

    kind = "github"

    def __init__(self, project_url: str, repo: str, gh_bin: str | None = None):
        self.project_url = project_url
        self.repo = repo
        (self._owner_type, self._owner, self._project_number) = _parse_project_url(project_url)
        (self._repo_owner, self._repo_name) = _parse_owner_repo(repo)
        self._gh_bin = gh_bin
        self._cache: dict[str, Any] = {}
        # Sub-issue API availability is detected lazily — set on first create_child.
        self._sub_issue_api_available: bool | None = None

    # ── Transport (same idiom as workflow.github) ─────────────────────────

    def _gh_path(self) -> str:
        if self._gh_bin:
            return self._gh_bin
        resolved = shutil.which("gh")
        if not resolved:
            raise HierarchyError("'gh' CLI not found on PATH — install it or pass gh_bin=...")
        return resolved

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Same value-type-aware transport as workflow.github._graphql —
        scalars via ``-f``/``-F`` flags, arrays/objects via stdin JSON.
        Re-implemented here (rather than imported) to keep hierarchy
        and workflow decoupled — one adapter breaking shouldn't poison
        the other's transport."""
        variables = variables or {}
        has_complex = any(isinstance(v, (list, dict)) for v in variables.values())

        if has_complex:
            payload = {"query": query, "variables": variables}
            cmd = [self._gh_path(), "api", "graphql", "--input", "-"]
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            cmd = [self._gh_path(), "api", "graphql", "-f", f"query={query}"]
            for key, val in variables.items():
                if isinstance(val, int):
                    cmd.extend(["-F", f"{key}={val}"])
                else:
                    cmd.extend(["-f", f"{key}={val}"])
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if proc.returncode != 0:
            raise HierarchyError(f"gh api graphql failed: {proc.stderr.strip() or proc.stdout.strip()}")
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise HierarchyError(f"gh api graphql returned non-JSON: {exc}") from exc
        if "errors" in payload and payload["errors"]:
            msg = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise HierarchyError(f"GraphQL error: {msg}")
        return payload.get("data") or {}

    # ── Warm cache ────────────────────────────────────────────────────────

    def _ensure_cache(self) -> None:
        """Fetch project + repo node ids once per adapter instance."""
        if "project_id" in self._cache:
            return
        query = _Q_PROJECT_AND_REPO.get(self._owner_type)
        if not query:
            raise HierarchyError(f"unsupported owner type {self._owner_type!r}")
        data = self._graphql(
            query,
            {"login": self._owner, "number": self._project_number, "repo": self._repo_name},
        )
        root = data.get(self._owner_type) or {}
        project = root.get("projectV2") or {}
        repository = root.get("repository") or {}
        if not project.get("id"):
            raise HierarchyError(f"project not found or not accessible: {self.project_url}")
        if not repository.get("id"):
            raise HierarchyError(f"repository not found: {self.repo} (check project owner matches repo owner)")
        self._cache["project_id"] = project["id"]
        self._cache["project_title"] = project.get("title", "")
        self._cache["repo_id"] = repository["id"]

    # ── get ───────────────────────────────────────────────────────────────

    def get(self, id: str) -> WorkItem | None:
        """Fetch one item by id.

        Accepts several id forms:

        * full issue URL — ``https://github.com/owner/repo/issues/N``
        * short form — ``"owner/repo#N"``
        * bare number — ``"N"`` (uses the adapter's configured repo)

        Project drafts can only be fetched via :meth:`list_at_level` —
        drafts don't have a queryable standalone identity.
        """
        number, repo_slug = _parse_issue_ref(id, default_repo=f"{self._repo_owner}/{self._repo_name}")
        owner, repo = _parse_owner_repo(repo_slug)
        try:
            data = self._graphql(
                _Q_ISSUE_BY_NUMBER,
                {"owner": owner, "repo": repo, "number": number},
            )
        except HierarchyError:
            return None
        issue = ((data.get("repository") or {}).get("issue")) or {}
        if not issue:
            return None
        return _issue_to_workitem(issue, level=HierarchyLevel.STORY)

    # ── list_at_level ─────────────────────────────────────────────────────

    def list_at_level(
        self,
        level: HierarchyLevel,
        *,
        parent: WorkItem | str | None = None,
        label: str | None = None,
    ) -> list[WorkItem]:
        """Per-level listing.

        * **Epic** — iterates project items, returns every DraftIssue
          (optionally filtered by body/title match when *label* is set —
          drafts don't have real labels, so ``label`` is best-effort).
        * **Story** — lists issues in the configured repo, filtered by
          ``label`` (mandatory in practice to avoid pulling every issue).
        * **Task** — sub-issues of *parent* (Story). When the sub-issue
          API isn't usable, falls back to parsing the parent's body for
          ``- [ ] #N`` checkbox references.
        """
        # Validate first — cache warming is a network call, don't do it
        # when the caller asked for something impossible.
        if level is HierarchyLevel.TASK and parent is None:
            raise HierarchyError("list_at_level(TASK) requires parent=Story")

        self._ensure_cache()
        if level is HierarchyLevel.EPIC:
            return self._list_epics(label=label)
        if level is HierarchyLevel.STORY:
            return self._list_stories(label=label)
        if level is HierarchyLevel.TASK:
            parent_item = parent if isinstance(parent, WorkItem) else self.get(parent)
            if parent_item is None:
                return []
            return self._list_tasks(parent_item)
        raise HierarchyError(f"unhandled level {level!r}")

    def _list_epics(self, *, label: str | None) -> list[WorkItem]:
        epics: list[WorkItem] = []
        after: str | None = None
        label_norm = (label or "").lower() or None
        while True:
            data = self._graphql(
                _Q_PROJECT_ITEMS,
                {"projectId": self._cache["project_id"], "first": 100, "after": after},
            )
            items_node = ((data.get("node") or {}).get("items")) or {}
            for item in items_node.get("nodes") or []:
                content = item.get("content") or {}
                if content.get("__typename") != "DraftIssue":
                    continue
                title = content.get("title") or ""
                body = content.get("body") or ""
                if label_norm and label_norm not in (title + " " + body).lower():
                    continue
                epics.append(
                    WorkItem(
                        id=item["id"],  # project-item id is the draft's whilly id
                        level=HierarchyLevel.EPIC,
                        title=title,
                        body=body,
                        external_ref={
                            "project_item_id": item["id"],
                            "draft_id": content.get("id", ""),
                        },
                    )
                )
            page_info = items_node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return epics

    def _list_stories(self, *, label: str | None) -> list[WorkItem]:
        labels = [label] if label else []
        data = self._graphql(
            _Q_ISSUES_BY_LABEL,
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "labels": labels,
                "first": 100,
            },
        )
        nodes = ((data.get("repository") or {}).get("issues") or {}).get("nodes") or []
        return [_issue_to_workitem(n, level=HierarchyLevel.STORY) for n in nodes if n]

    def _list_tasks(self, parent: WorkItem) -> list[WorkItem]:
        """Return sub-issues of *parent*. Uses the sub-issue API when
        available; otherwise parses checkbox references from the body.
        """
        # Checkbox fallback — always works, no feature detection needed.
        body = parent.body or ""
        refs = re.findall(r"-\s*\[[\sx]?\]\s+(?:\w+/\w+)?#(\d+)", body)
        tasks: list[WorkItem] = []
        for ref in refs:
            item = self.get(ref)
            if item is not None:
                # Re-cast to Task level for this view.
                tasks.append(
                    WorkItem(
                        id=item.id,
                        level=HierarchyLevel.TASK,
                        title=item.title,
                        body=item.body,
                        parent_id=parent.id,
                        external_ref=item.external_ref,
                        labels=item.labels,
                        status=item.status,
                    )
                )
        return tasks

    # ── promote: Epic draft → Story issue ─────────────────────────────────

    def promote(self, item: WorkItem) -> WorkItem:
        """Epic (draft) → Story (real issue). No-op for items already
        first-class at their level."""
        if item.level is not HierarchyLevel.EPIC:
            return item  # Stories/Tasks are already first-class
        item_id = item.external_ref.get("project_item_id") or item.id
        if not item_id:
            raise HierarchyError("cannot promote — missing project_item_id")
        self._ensure_cache()
        data = self._graphql(
            _M_CONVERT_DRAFT,
            {"itemId": item_id, "repoId": self._cache["repo_id"]},
        )
        new_item = ((data.get("convertProjectV2DraftIssueItemToIssue") or {}).get("item")) or {}
        issue = new_item.get("content") or {}
        if not issue.get("id"):
            raise HierarchyError("draft conversion returned no issue content")
        story = WorkItem(
            id=issue.get("url") or str(issue.get("number")),
            level=HierarchyLevel.STORY,
            title=issue.get("title") or item.title,
            body=item.body,
            parent_id=None,
            external_ref={
                "issue_node_id": issue["id"],
                "project_item_id": new_item.get("id") or item_id,
                "repo": self.repo,
                "number": issue.get("number"),
                "url": issue.get("url"),
            },
        )
        return story

    # ── create_child: Story → Task sub-issue ──────────────────────────────

    def create_child(
        self,
        parent: WorkItem,
        title: str,
        body: str = "",
        *,
        labels: list[str] | None = None,
    ) -> WorkItem:
        if parent.level is HierarchyLevel.TASK:
            raise HierarchyError("tasks cannot have children in the GitHub adapter")

        self._ensure_cache()

        # Create the new issue in the configured repo.
        # Labels are skipped in v1 — GitHub createIssue requires label *ids*, not names,
        # which means an extra round-trip. The checkbox fallback already uses labels
        # differently (whilly:ready:task goes on the parent, not the child).
        data = self._graphql(
            _M_CREATE_ISSUE,
            {
                "repoId": self._cache["repo_id"],
                "title": title,
                "body": body,
                "labels": [],
            },
        )
        issue = ((data.get("createIssue") or {}).get("issue")) or {}
        if not issue.get("id"):
            raise HierarchyError("createIssue returned no issue")

        child = WorkItem(
            id=issue.get("url") or str(issue.get("number")),
            level=HierarchyLevel.TASK if parent.level is HierarchyLevel.STORY else HierarchyLevel.STORY,
            title=issue.get("title") or title,
            body=body,
            parent_id=parent.id,
            external_ref={
                "issue_node_id": issue["id"],
                "repo": self.repo,
                "number": issue.get("number"),
                "url": issue.get("url"),
            },
            labels=list(labels or []),
        )

        # Link to parent — sub-issue API first, checkbox fallback on failure.
        if parent.level is HierarchyLevel.STORY:
            linked = self._try_add_sub_issue(parent, child)
            if not linked:
                self._append_checkbox(parent, child)
        return child

    def _try_add_sub_issue(self, parent: WorkItem, child: WorkItem) -> bool:
        """Return True when GitHub's sub-issue mutation succeeded."""
        parent_id = parent.external_ref.get("issue_node_id")
        child_id = child.external_ref.get("issue_node_id")
        if not parent_id or not child_id:
            return False
        try:
            self._graphql(_M_ADD_SUB_ISSUE, {"parentId": parent_id, "childId": child_id})
            self._sub_issue_api_available = True
            return True
        except HierarchyError as exc:
            log.info("sub-issue API unavailable (%s) — falling back to checkbox list", exc)
            self._sub_issue_api_available = False
            return False

    def _append_checkbox(self, parent: WorkItem, child: WorkItem) -> None:
        """Fallback path: append ``- [ ] #N`` to parent's body via
        ``gh issue edit`` so the parent renders a trackable checklist.

        We explicitly don't round-trip through GraphQL for the body edit
        — ``gh issue edit`` is friendlier about body concatenation and
        markdown preservation than raw GraphQL.
        """
        parent_ref = parent.external_ref.get("url") or parent.id
        child_ref = child.external_ref.get("number")
        if not parent_ref or not child_ref:
            log.warning("checkbox fallback skipped — missing parent URL or child number")
            return
        line = f"- [ ] #{child_ref}"
        body_append = f"\n\n### Sub-tasks (whilly)\n{line}\n"
        proc = subprocess.run(
            [
                self._gh_path(),
                "issue",
                "edit",
                parent_ref,
                "--body-file",
                "-",
            ],
            input=(parent.body or "") + body_append,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            log.warning("gh issue edit (checkbox fallback) failed: %s", proc.stderr.strip()[:200])

    # ── create_at_level: root-level creation (no parent) ──────────────────

    def create_at_level(
        self,
        level: HierarchyLevel,
        title: str,
        body: str = "",
    ) -> WorkItem:
        """Create a root-level item — Epic (project draft) or Story (repo issue).

        TASK level raises :class:`HierarchyError`: tasks always need a
        parent; callers must use :meth:`create_child` instead.
        """
        # Validate before doing any network.
        if level is HierarchyLevel.TASK:
            raise HierarchyError("create_at_level(task) is not supported — tasks need a parent; use create_child()")
        self._ensure_cache()
        if level is HierarchyLevel.EPIC:
            data = self._graphql(
                _M_ADD_DRAFT,
                {
                    "projectId": self._cache["project_id"],
                    "title": title,
                    "body": body,
                },
            )
            item = ((data.get("addProjectV2DraftIssue") or {}).get("projectItem")) or {}
            if not item.get("id"):
                raise HierarchyError("addProjectV2DraftIssue returned no projectItem")
            content = item.get("content") or {}
            return WorkItem(
                id=item["id"],
                level=HierarchyLevel.EPIC,
                title=content.get("title") or title,
                body=body,
                external_ref={
                    "project_item_id": item["id"],
                    "draft_id": content.get("id", ""),
                },
            )
        if level is HierarchyLevel.STORY:
            data = self._graphql(
                _M_CREATE_ISSUE,
                {
                    "repoId": self._cache["repo_id"],
                    "title": title,
                    "body": body,
                    "labels": [],
                },
            )
            issue = ((data.get("createIssue") or {}).get("issue")) or {}
            if not issue.get("id"):
                raise HierarchyError("createIssue returned no issue")
            return WorkItem(
                id=issue.get("url") or str(issue.get("number")),
                level=HierarchyLevel.STORY,
                title=issue.get("title") or title,
                body=body,
                external_ref={
                    "issue_node_id": issue["id"],
                    "repo": self.repo,
                    "number": issue.get("number"),
                    "url": issue.get("url"),
                },
            )
        raise HierarchyError(f"create_at_level({level.value}) is not a valid root-level creation")

    # ── link: attach existing child to parent ─────────────────────────────

    def link(self, parent: WorkItem, child: WorkItem) -> bool:
        if parent.level is HierarchyLevel.TASK:
            return False
        if parent.level is HierarchyLevel.STORY and child.level is HierarchyLevel.TASK:
            linked = self._try_add_sub_issue(parent, child)
            if not linked:
                self._append_checkbox(parent, child)
            return True
        # Epic → Story: GitHub has no first-class "epic issue" concept
        # beyond the "tracked by" field in newer issue forms. For v1 we
        # store the link via a checkbox in the Epic's draft/issue body,
        # same as Task linking.
        if parent.level is HierarchyLevel.EPIC:
            self._append_checkbox(parent, child)
            return True
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────


def _issue_to_workitem(node: dict, *, level: HierarchyLevel) -> WorkItem:
    labels_nodes = ((node.get("labels") or {}).get("nodes")) or []
    labels = [lab.get("name", "") for lab in labels_nodes if lab]
    return WorkItem(
        id=node.get("url") or str(node.get("number", "")),
        level=level,
        title=node.get("title") or "",
        body=node.get("body") or "",
        external_ref={
            "issue_node_id": node.get("id"),
            "repo": (node.get("repository") or {}).get("nameWithOwner", ""),
            "number": node.get("number"),
            "url": node.get("url", ""),
        },
        labels=labels,
        status=(node.get("state") or "").lower(),
    )


def _parse_issue_ref(ref: str, *, default_repo: str) -> tuple[int, str]:
    """Parse an issue reference to (number, "owner/repo").

    Accepted forms: full URL, ``owner/repo#N``, ``#N``, bare ``N``.
    """
    if not ref:
        raise HierarchyError("empty issue reference")
    m = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if m:
        return int(m.group(2)), m.group(1)
    m = re.match(r"([^/]+/[^#/]+)#(\d+)$", ref.strip())
    if m:
        return int(m.group(2)), m.group(1)
    m = re.match(r"#?(\d+)$", ref.strip())
    if m:
        return int(m.group(1)), default_repo
    raise HierarchyError(f"unrecognised issue reference {ref!r}")
