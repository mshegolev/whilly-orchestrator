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
                    options { id name color description }
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
                    options { id name color description }
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

# Append a new option to a SingleSelect field.
#
# GitHub's ``updateProjectV2Field.singleSelectOptions`` **replaces** the whole
# options list. To add a column without wiping existing ones we resend every
# current option (with its id, so items keep their assignments) plus the new
# one (no id → created). Colors are an enum; we let the caller pick GRAY by
# default to avoid accidental semantic colouring.
_M_UPDATE_STATUS_OPTIONS = """
    mutation($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
      updateProjectV2Field(input: {
        fieldId: $fieldId,
        singleSelectOptions: $options
      }) {
        projectV2Field {
          ... on ProjectV2SingleSelectField {
            id
            options { id name color description }
          }
        }
      }
    }
"""


# Valid colour values for ProjectV2SingleSelectFieldOptionColor. Kept as a
# tuple so tests can assert membership without importing from GitHub docs.
_VALID_OPTION_COLORS: tuple[str, ...] = (
    "GRAY",
    "BLUE",
    "GREEN",
    "YELLOW",
    "ORANGE",
    "RED",
    "PINK",
    "PURPLE",
)


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
        """Run a GraphQL request via ``gh api graphql`` and return the parsed
        ``data`` object. Raises :class:`RuntimeError` on non-zero exit or
        GraphQL-level errors.

        Variable marshalling is value-type-aware:

        * scalars (``str``, ``int``) go as ``-f`` / ``-F`` command-line vars —
          the simple path, used by every query.
        * ``list``/``dict`` values trigger the JSON-body path — ``gh api
          graphql --input -`` with a full ``{query, variables}`` payload on
          stdin. This is the only way to pass array/object inputs like the
          ``singleSelectOptions`` array that :meth:`add_status` needs.
        """
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
        raw_options = [
            {
                "id": opt["id"],
                "name": opt["name"],
                "color": (opt.get("color") or "GRAY").upper(),
                "description": opt.get("description") or "",
            }
            for opt in (status_field.get("options") or [])
        ]
        # Keep both shapes: BoardStatus for the Protocol surface, raw dicts for
        # the preserve-then-append merge that add_status does (GitHub replaces
        # the entire option list on every mutation — see _M_UPDATE_STATUS_OPTIONS).
        self._cache["raw_options"] = raw_options
        self._cache["statuses"] = [BoardStatus(id=opt["id"], name=opt["name"]) for opt in raw_options]

    # ── Protocol surface ──────────────────────────────────────────────────

    def list_statuses(self) -> list[BoardStatus]:
        self._ensure_cached()
        return list(self._cache["statuses"])

    def add_status(self, name: str, color: str = "GRAY", description: str = "Added by whilly") -> BoardStatus:
        """Create a new Status option on the board via GraphQL mutation.

        ``updateProjectV2Field.singleSelectOptions`` *replaces* the whole
        options array — we resend every current option (with its id, so
        items keep their assignment) and append the new one.

        Args:
            name: column label to create. Matched case-insensitively against
                existing columns; a hit returns the existing :class:`BoardStatus`
                without mutating (idempotent).
            color: one of :data:`_VALID_OPTION_COLORS`. Default GRAY — the
                caller (proposer) doesn't currently customise colours; future
                lifecycle-event-aware colouring is a clean extension.
            description: free-text description stored on the option.

        Raises:
            ValueError: ``color`` isn't a valid GitHub option colour.
            RuntimeError: GraphQL mutation failed (permissions, API change,
                network). The proposer's interactive flow catches this and
                falls back to map-existing.
        """
        color = (color or "GRAY").upper()
        if color not in _VALID_OPTION_COLORS:
            raise ValueError(f"invalid option color {color!r} — must be one of {', '.join(_VALID_OPTION_COLORS)}")

        self._ensure_cached()

        # Idempotent: existing name (case-insensitive) short-circuits the mutation.
        target_lower = name.strip().lower()
        for st in self._cache["statuses"]:
            if st.name.lower() == target_lower:
                return st

        # GitHub's ProjectV2SingleSelectFieldOptionInput does NOT accept `id` —
        # the mutation matches by `name` for preservation. Sending the full
        # list (existing options + the new one) keeps prior assignments
        # intact because the server re-links items by option name.
        existing_raw = self._cache["raw_options"]
        merged_options = [
            {
                "name": opt["name"],
                "color": opt["color"],
                "description": opt["description"],
            }
            for opt in existing_raw
        ] + [
            {
                "name": name,
                "color": color,
                "description": description,
            }
        ]

        data = self._graphql(
            _M_UPDATE_STATUS_OPTIONS,
            {"fieldId": self._cache["status_field_id"], "options": merged_options},
        )

        # Parse returned options list, find the newly-created one by name.
        returned = (((data.get("updateProjectV2Field") or {}).get("projectV2Field") or {}).get("options")) or []
        new_opt: dict[str, Any] | None = None
        for opt in returned:
            if (opt.get("name") or "").lower() == target_lower:
                # Prefer an id we didn't already know about — handles the race
                # where our name collides with a just-added duplicate.
                known_ids = {e["id"] for e in existing_raw}
                if opt.get("id") and opt["id"] not in known_ids:
                    new_opt = opt
                    break
                # Fallback: accept the match even if we can't disambiguate
                # (should be rare — duplicate check above usually prevents it).
                new_opt = opt

        if not new_opt or not new_opt.get("id"):
            # Mutation succeeded but we can't locate the new option — refresh
            # the cache and re-scan so the caller always gets a usable handle.
            self._cache.clear()
            self._ensure_cached()
            for st in self._cache["statuses"]:
                if st.name.lower() == target_lower:
                    return st
            raise RuntimeError(f"add_status: mutation succeeded but option {name!r} not found in response")

        new_status = BoardStatus(id=new_opt["id"], name=new_opt["name"])
        # Refresh the cached option lists so the next mutation merges against
        # the updated set.
        self._cache["raw_options"] = [
            {
                "id": opt["id"],
                "name": opt["name"],
                "color": (opt.get("color") or "GRAY").upper(),
                "description": opt.get("description") or "",
            }
            for opt in returned
        ]
        self._cache["statuses"] = [BoardStatus(id=opt["id"], name=opt["name"]) for opt in returned]
        return new_status

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
