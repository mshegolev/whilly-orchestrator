"""GitHub Projects v2 adapter — the first concrete :class:`BoardSink` impl.

Projects v2 are GraphQL-only (REST doesn't support them), so every call here
goes through ``gh api graphql`` — reuses the user's existing ``gh`` auth
without pulling a new HTTP client into the deps.

URL parsing accepts all three flavours:

* ``https://github.com/users/{owner}/projects/{N}``   — user-level project
* ``https://github.com/orgs/{owner}/projects/{N}``    — org-level project
* ``https://github.com/{owner}/{repo}/projects/{N}``  — repo-level (classic/v2)

Graceful-degradation discipline:

* :meth:`list_statuses` raises :class:`RuntimeError` with a clear message
  when ``gh`` is missing, the project is inaccessible, or the Status field
  doesn't exist — the analyzer expects to fail fast there.
* :meth:`move_item` returns ``False`` on any error and logs — pipeline
  doesn't crash because a card couldn't move.
* :meth:`add_status` may raise :class:`RuntimeError` when the mutation
  fails (permission denied is the common cause — the proposer catches and
  falls back to "map existing").
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from whilly.workflow.base import BoardStatus

log = logging.getLogger("whilly.workflow.github")


# ── URL parsing ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectRef:
    """Parsed form of a GitHub Projects v2 URL."""

    owner_type: str  # "user" | "organization" | "repository"
    owner: str
    number: int
    repo: str | None = None  # only set for repo-level URLs


_PATTERNS = [
    (re.compile(r"github\.com/users/([^/]+)/projects/(\d+)"), "user"),
    (re.compile(r"github\.com/orgs/([^/]+)/projects/(\d+)"), "organization"),
    (re.compile(r"github\.com/([^/]+)/([^/]+)/projects/(\d+)"), "repository"),
]


def parse_project_url(url: str) -> ProjectRef:
    """Parse a GitHub Projects v2 URL into a :class:`ProjectRef`.

    Raises:
        ValueError: URL doesn't match any supported pattern.
    """
    if not url:
        raise ValueError("empty project URL")
    for pattern, kind in _PATTERNS:
        m = pattern.search(url)
        if not m:
            continue
        if kind == "repository":
            owner, repo, number = m.group(1), m.group(2), int(m.group(3))
            return ProjectRef(owner_type=kind, owner=owner, repo=repo, number=number)
        owner, number = m.group(1), int(m.group(2))
        return ProjectRef(owner_type=kind, owner=owner, number=number)
    raise ValueError(
        f"unrecognised GitHub Projects URL: {url!r} — expected one of "
        "github.com/users/<owner>/projects/<N>, "
        "github.com/orgs/<owner>/projects/<N>, or "
        "github.com/<owner>/<repo>/projects/<N>"
    )


# ── GraphQL queries ───────────────────────────────────────────────────────────


# Resolves Project by owner kind + number, returns id + Status field + options.
_Q_PROJECT_INFO = {
    "user": """
        query($login: String!, $number: Int!) {
          user(login: $login) {
            projectV2(number: $number) {
              id
              title
              fields(first: 50) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options { id name }
                  }
                }
              }
            }
          }
        }
    """,
    "organization": """
        query($login: String!, $number: Int!) {
          organization(login: $login) {
            projectV2(number: $number) {
              id
              title
              fields(first: 50) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options { id name }
                  }
                }
              }
            }
          }
        }
    """,
}

# Find the ProjectV2Item corresponding to an issue (given project id + issue
# URL). Paginates up to 100 items which is enough for most boards; v2 iterator
# could be added if this becomes a limit.
_Q_ITEM_BY_ISSUE = """
    query($projectId: ID!, $first: Int!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: $first) {
            nodes {
              id
              content {
                ... on Issue { number url repository { nameWithOwner } }
                ... on PullRequest { number url repository { nameWithOwner } }
              }
            }
          }
        }
      }
    }
"""

# Move an item: set its Status field to a specific option id.
_M_SET_STATUS = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId,
        itemId: $itemId,
        fieldId: $fieldId,
        value: { singleSelectOptionId: $optionId }
      }) { projectV2Item { id } }
    }
"""

# Create a new option on a SingleSelect field.
_M_ADD_STATUS_OPTION = """
    mutation($fieldId: ID!, $name: String!) {
      updateProjectV2Field(input: {
        fieldId: $fieldId,
        singleSelectOptions: [{ name: $name, color: GRAY, description: "Added by whilly" }]
      }) { projectV2Field { __typename } }
    }
"""


# ── The adapter ───────────────────────────────────────────────────────────────


class GitHubProjectBoard:
    """:class:`BoardSink` impl backed by GitHub Projects v2 via ``gh api graphql``."""

    kind = "github_project"

    def __init__(self, url: str, gh_bin: str | None = None):
        """Args:
        url: full project URL (see :func:`parse_project_url`).
        gh_bin: override for the ``gh`` binary path — defaults to ``gh`` on PATH.
            Resolved lazily on first call so unit tests can construct an
            instance without ``gh`` being installed.
        """
        self.url = url
        self.ref = parse_project_url(url)
        self._gh_bin = gh_bin
        # Cached after the first list_statuses(): (project_id, status_field_id, statuses)
        self._cache: dict[str, Any] = {}

    # ── Transport ─────────────────────────────────────────────────────────

    def _gh_path(self) -> str:
        if self._gh_bin:
            return self._gh_bin
        resolved = shutil.which("gh")
        if not resolved:
            raise RuntimeError("'gh' CLI not found on PATH — install GitHub CLI or pass gh_bin=...")
        return resolved

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL query via ``gh api graphql`` and return the parsed
        ``data`` object. Raises :class:`RuntimeError` on non-zero exit or
        GraphQL-level errors."""
        cmd = [self._gh_path(), "api", "graphql", "-f", f"query={query}"]
        for key, val in (variables or {}).items():
            # gh api expects ints as -F and strings as -f. We route by type.
            if isinstance(val, int):
                cmd.extend(["-F", f"{key}={val}"])
            else:
                cmd.extend(["-f", f"{key}={val}"])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"gh api graphql failed: {proc.stderr.strip() or proc.stdout.strip()}")
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh api graphql returned non-JSON: {exc}") from exc
        if "errors" in payload and payload["errors"]:
            msg = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise RuntimeError(f"GraphQL error: {msg}")
        return payload.get("data") or {}

    # ── Introspection ─────────────────────────────────────────────────────

    def _fetch_project_info(self) -> dict[str, Any]:
        """Populate ``self._cache`` with project id, status field id, options."""
        owner_type = self.ref.owner_type
        if owner_type == "repository":
            # Repo-level projects v2 are actually owned by user/org; we still
            # need to pick one. Probe user first, then org.
            for kind in ("user", "organization"):
                try:
                    data = self._graphql(
                        _Q_PROJECT_INFO[kind],
                        {"login": self.ref.owner, "number": self.ref.number},
                    )
                    root = data.get(kind) or {}
                    if root.get("projectV2"):
                        self._cache["_kind_resolved"] = kind
                        return root["projectV2"]
                except RuntimeError as exc:
                    log.debug("project lookup failed for %s: %s", kind, exc)
            raise RuntimeError(f"no ProjectV2 found at {self.url}")
        query = _Q_PROJECT_INFO.get(owner_type)
        if query is None:
            raise RuntimeError(f"unsupported owner_type: {owner_type!r}")
        data = self._graphql(query, {"login": self.ref.owner, "number": self.ref.number})
        root = (data.get(owner_type) or {}).get("projectV2")
        if not root:
            raise RuntimeError(f"project not found or not accessible: {self.url}")
        return root

    def _ensure_cached(self) -> None:
        if "project_id" in self._cache:
            return
        info = self._fetch_project_info()
        self._cache["project_id"] = info["id"]
        self._cache["title"] = info.get("title", "")
        status_field = None
        for node in (info.get("fields", {}) or {}).get("nodes") or []:
            if not node:
                continue
            # The __typename check happens in the query (inline fragment) — if
            # node has 'options', it's the SingleSelect field.
            if node.get("name", "").lower() == "status" and "options" in node:
                status_field = node
                break
        if not status_field:
            raise RuntimeError(
                f"project {self.url} has no 'Status' single-select field — create one on the board first"
            )
        self._cache["status_field_id"] = status_field["id"]
        self._cache["statuses"] = [
            BoardStatus(id=opt["id"], name=opt["name"]) for opt in (status_field.get("options") or [])
        ]

    # ── Protocol surface ──────────────────────────────────────────────────

    def list_statuses(self) -> list[BoardStatus]:
        self._ensure_cached()
        return list(self._cache["statuses"])

    def add_status(self, name: str) -> BoardStatus:
        """Create a new Status option via GraphQL mutation.

        GitHub's API on ``updateProjectV2Field.singleSelectOptions`` *replaces*
        the full list — so we resend existing options plus the new one.
        """
        self._ensure_cached()
        existing = self._cache["statuses"]
        # Refuse duplicates (case-insensitive).
        for st in existing:
            if st.name.lower() == name.lower():
                return st
        # updateProjectV2Field replaces the whole options array — rebuild it.
        merged = [{"name": s.name} for s in existing] + [{"name": name}]
        raise NotImplementedError(
            "add_status is pending GraphQL schema verification — "
            f"cannot yet create status option {name!r} "
            f"(would be set alongside {len(existing)} existing options: {merged}). "
            "For now, add the column manually on the board UI and re-run analyze."
        )

    def move_item(self, issue_ref: str, status: BoardStatus) -> bool:
        try:
            self._ensure_cached()
            item_id = self._find_item_id(issue_ref)
            if not item_id:
                log.warning("move_item: no project item found for %s", issue_ref)
                return False
            self._graphql(
                _M_SET_STATUS,
                {
                    "projectId": self._cache["project_id"],
                    "itemId": item_id,
                    "fieldId": self._cache["status_field_id"],
                    "optionId": status.id,
                },
            )
            log.info("moved %s → %s", issue_ref, status.name)
            return True
        except (RuntimeError, ValueError) as exc:
            log.warning("move_item failed for %s → %s: %s", issue_ref, status.name, exc)
            return False

    # ── Item lookup ───────────────────────────────────────────────────────

    def _find_item_id(self, issue_ref: str) -> str | None:
        """Resolve ``issue_ref`` to a ProjectV2Item id by paging project items.

        Accepted forms:

        * full issue URL ``https://github.com/owner/repo/issues/42``
        * ``owner/repo#42``
        * plain ``#42`` or ``42`` — only usable when the project has items
          from a single repo; we match by issue number alone.
        """
        target_number, target_nameWithOwner = _parse_issue_ref(issue_ref)
        data = self._graphql(
            _Q_ITEM_BY_ISSUE,
            {"projectId": self._cache["project_id"], "first": 100},
        )
        items = ((data.get("node") or {}).get("items") or {}).get("nodes") or []
        for item in items:
            content = item.get("content") or {}
            if not content:
                continue
            if content.get("number") != target_number:
                continue
            if target_nameWithOwner:
                repo = (content.get("repository") or {}).get("nameWithOwner")
                if repo and repo != target_nameWithOwner:
                    continue
            return item["id"]
        return None


def _parse_issue_ref(ref: str) -> tuple[int, str | None]:
    """Split an issue reference into (number, "owner/repo" or None)."""
    if not ref:
        raise ValueError("empty issue reference")
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", ref)
    if m:
        return int(m.group(3)), f"{m.group(1)}/{m.group(2)}"
    m = re.match(r"([^/]+/[^#/]+)#(\d+)$", ref.strip())
    if m:
        return int(m.group(2)), m.group(1)
    m = re.match(r"#?(\d+)$", ref.strip())
    if m:
        return int(m.group(1)), None
    raise ValueError(f"unrecognised issue reference: {ref!r}")
