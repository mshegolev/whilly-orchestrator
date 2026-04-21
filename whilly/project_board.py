"""GitHub Projects v2 board client — move cards across statuses as tasks progress.

Lightweight wrapper around the ``updateProjectV2ItemFieldValue`` GraphQL mutation.
Caches project metadata (project id, Status field id, option ids, item → issue
mapping) on first use so subsequent transitions are one API call.

Typical usage from the orchestrator::

    client = ProjectBoardClient.from_config(config)
    client.set_issue_status(162, "mshegolev/whilly-orchestrator", "In Progress")

If the board doesn't know about the issue, or the status option doesn't exist,
:meth:`set_issue_status` returns False rather than raising — card movement is a
best-effort surface, not a reason to fail a run.

Requires ``gh`` CLI with the ``project`` scope (``gh auth refresh -s project``).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from whilly.gh_utils import gh_subprocess_env

log = logging.getLogger("whilly")


# Whilly's internal task statuses → default GitHub Projects v2 Status option name.
# Override per project via config if your board uses different columns.
DEFAULT_STATUS_MAPPING: dict[str, str] = {
    "pending": "Todo",
    "in_progress": "In Progress",
    "done": "In Review",  # PR usually still open when task hits 'done'
    "merged": "Done",  # synthetic post-merge state signalled by the merge flow
    "failed": "Failed",
    "skipped": "Refused",
}


@dataclass
class _ProjectMeta:
    project_id: str
    status_field_id: str
    option_id_by_name: dict[str, str]
    item_id_by_key: dict[tuple[str, int], str] = field(default_factory=dict)


class ProjectBoardClient:
    """Thin GraphQL client for a single Projects v2 board."""

    def __init__(
        self,
        project_url: str,
        status_mapping: dict[str, str] | None = None,
        *,
        default_repo: str | None = None,
    ) -> None:
        self.project_url = project_url
        self.status_mapping = dict(DEFAULT_STATUS_MAPPING)
        if status_mapping:
            self.status_mapping.update(status_mapping)
        self.default_repo = default_repo
        self._owner, self._owner_type, self._number = self._parse_url(project_url)
        self._meta: _ProjectMeta | None = None

    # ── Construction from layered config ──────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any) -> ProjectBoardClient | None:
        """Build a client from ``WhillyConfig`` + ``get_toml_section("project_board")``.

        Returns ``None`` when the board integration is disabled or unconfigured.
        The orchestrator treats a ``None`` return as a no-op — no hook is wired.
        """
        try:
            from whilly.config import get_toml_section
        except ImportError:
            return None
        section = get_toml_section("project_board")
        url = (section.get("url") or "").strip()
        enabled = bool(section.get("enabled", bool(url)))
        if not (url and enabled):
            return None
        mapping = section.get("status_mapping")
        default_repo = section.get("default_repo") or None
        return cls(url, status_mapping=mapping if isinstance(mapping, dict) else None, default_repo=default_repo)

    # ── Public surface ────────────────────────────────────────────────────────

    def set_issue_status(self, issue_number: int, repo: str | None, status_name: str) -> bool:
        """Move the card for ``repo#issue_number`` to the column named ``status_name``.

        Returns True on success, False on any soft failure (card not on board, status
        not configured, gh error). Never raises — board sync is advisory.
        """
        try:
            meta = self._load_meta()
        except Exception as exc:
            log.warning("Project board metadata fetch failed — skipping card move: %s", exc)
            return False

        option_id = meta.option_id_by_name.get(status_name)
        if not option_id:
            log.warning(
                "Project board has no status option %r — available: %s",
                status_name,
                sorted(meta.option_id_by_name),
            )
            return False

        lookup_repo = repo or self.default_repo or ""
        item_id = meta.item_id_by_key.get((lookup_repo, issue_number))
        if not item_id and not repo:
            # Fall back to any repo if caller didn't supply one.
            item_id = next(
                (iid for (_r, num), iid in meta.item_id_by_key.items() if num == issue_number),
                None,
            )
        if not item_id:
            log.info("Issue #%d not on project board — skipping card move", issue_number)
            return False

        return self._update_status(meta.project_id, item_id, meta.status_field_id, option_id, status_name, issue_number)

    def set_task_status(self, task: Any, whilly_status: str) -> bool:
        """Translate a whilly status → board column and move the card for the task's issue.

        Task's GitHub issue is inferred from its ``id`` (``"GH-<N>"``) or from
        ``prd_requirement`` URLs like ``https://github.com/owner/repo/issues/N``.
        """
        mapped = self.status_mapping.get(whilly_status)
        if not mapped:
            return False
        issue_number, repo = self._extract_issue_ref(task)
        if issue_number is None:
            return False
        return self.set_issue_status(issue_number, repo, mapped)

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, int]:
        for pattern, owner_type in (
            (r"github\.com/users/([^/]+)/projects/(\d+)", "user"),
            (r"github\.com/orgs/([^/]+)/projects/(\d+)", "organization"),
        ):
            m = re.search(pattern, url)
            if m:
                return m.group(1), owner_type, int(m.group(2))
        raise ValueError(f"Unsupported project URL: {url!r}")

    @staticmethod
    def _extract_issue_ref(task: Any) -> tuple[int | None, str | None]:
        """Return ``(issue_number, "owner/repo" or None)`` for a task, if derivable."""
        task_id = getattr(task, "id", "") or ""
        match = re.match(r"^GH-(\d+)$", task_id)
        if match:
            number = int(match.group(1))
            prd = getattr(task, "prd_requirement", "") or ""
            repo_match = re.search(r"github\.com/([^/]+)/([^/]+)/issues/", prd)
            repo = f"{repo_match.group(1)}/{repo_match.group(2)}" if repo_match else None
            return number, repo
        return None, None

    def _load_meta(self) -> _ProjectMeta:
        if self._meta is not None:
            return self._meta
        data = self._gh_api(
            (
                "query($owner: String!, $number: Int!) {"
                f"  {self._owner_type}(login: $owner) {{"
                "    projectV2(number: $number) {"
                "      id"
                "      fields(first: 50) {"
                "        nodes {"
                "          __typename"
                "          ... on ProjectV2SingleSelectField { id name options { id name } }"
                "        }"
                "      }"
                "      items(first: 200) {"
                "        nodes {"
                "          id"
                "          content {"
                "            __typename"
                "            ... on Issue { number repository { nameWithOwner } }"
                "          }"
                "        }"
                "      }"
                "    }"
                "  }"
                "}"
            ),
            owner=self._owner,
            number=self._number,
        )
        project = data["data"][self._owner_type]["projectV2"]
        status_field = next(
            (
                n
                for n in project["fields"]["nodes"]
                if n.get("name") == "Status" and n.get("__typename") == "ProjectV2SingleSelectField"
            ),
            None,
        )
        if not status_field:
            raise RuntimeError("Project has no 'Status' single-select field")
        option_id_by_name = {opt["name"]: opt["id"] for opt in status_field["options"]}
        item_map: dict[tuple[str, int], str] = {}
        for node in project["items"]["nodes"]:
            content = node.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            repo = content["repository"]["nameWithOwner"]
            item_map[(repo, content["number"])] = node["id"]
        self._meta = _ProjectMeta(
            project_id=project["id"],
            status_field_id=status_field["id"],
            option_id_by_name=option_id_by_name,
            item_id_by_key=item_map,
        )
        return self._meta

    def _update_status(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
        status_name: str,
        issue_number: int,
    ) -> bool:
        try:
            self._gh_api(
                (
                    "mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {"
                    "  updateProjectV2ItemFieldValue("
                    "    input: { projectId: $project, itemId: $item, fieldId: $field,"
                    "             value: { singleSelectOptionId: $option } }"
                    "  ) { projectV2Item { id } }"
                    "}"
                ),
                project=project_id,
                item=item_id,
                field=field_id,
                option=option_id,
            )
        except Exception as exc:
            log.warning("Failed to move card for issue #%d to %r: %s", issue_number, status_name, exc)
            return False
        log.info("Project board: issue #%d → %r", issue_number, status_name)
        return True

    @staticmethod
    def _gh_api(query: str, **variables: Any) -> dict:
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args.extend(["-F", f"{key}={value}"])
        proc = subprocess.run(args, capture_output=True, text=True, env=gh_subprocess_env())
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "gh graphql failed").strip())
        return json.loads(proc.stdout or "{}")


__all__ = ["ProjectBoardClient", "DEFAULT_STATUS_MAPPING"]
