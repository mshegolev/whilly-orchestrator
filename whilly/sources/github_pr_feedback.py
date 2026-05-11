"""GitHub PR-feedback poller for the M2 PR-review feedback loop.

For every open ``pull_requests`` row under ``plan_id`` runs the
documented ``gh`` probe sequence:

1. ``gh pr view <n> --json reviewDecision,statusCheckRollup,latestReviews,reviewRequests,headRefOid,state``
2. ``gh api repos/{owner}/{repo}/pulls/{n}/reviews``
3. ``gh api repos/{owner}/{repo}/pulls/{n}/comments``

The triple is invoked in that order; deviation in flag set, ``--json``
field selection, or call ordering is asserted in the unit suite
(``tests/unit/test_pr_feedback_argv_shape.py``).

The poller diffs the per-PR cursors (``last_seen_review_id``,
``last_seen_check_run_id``) against the live response and emits at most
one event of each appropriate type per cycle:

* ``reviewDecision == 'APPROVED'`` ⇒ :data:`PR_REVIEW_APPROVED_EVENT_TYPE`
  with detail ``{pr_number, pr_url, head_sha, reviewer}``.
* ``reviewDecision == 'CHANGES_REQUESTED'`` ⇒
  :data:`PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE` with detail
  ``{pr_number, pr_url, head_sha, comments=[{body, path, line, author}, ...]}``
  — comment bodies are forwarded **verbatim** at this layer; sanitization
  for the downstream re-iterate prompt happens in
  :mod:`whilly.workflow.pr_iterate` (M2 feature ``m2-re-iterate-and-pr-fix``).
* ``state == 'MERGED'`` ⇒ :data:`PR_MERGED_EVENT_TYPE` with detail
  ``{pr_number, pr_url, head_sha, merged_at}`` and the
  ``pull_requests.state`` column flips to ``'merged'`` so the row drops
  out of the ``state='open'`` filter on the next cycle (VAL-PR-024 — no
  re-emit).

Failure handling
----------------
On ``gh`` non-zero exit OR :class:`subprocess.TimeoutExpired` the poll
for that PR is aborted: a WARNING containing the offending PR number is
logged, the cursor is **NOT** advanced (so the next successful cycle
resumes against the same baseline), and no event is emitted. Other PRs
in the same cycle still get their chance — failure is per-row, never
fatal to the cycle (VAL-PR-013).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from whilly.adapters.db.repository import (
    PR_MERGED_EVENT_TYPE,
    PR_REVIEW_APPROVED_EVENT_TYPE,
    PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
)
from whilly.gh_utils import gh_subprocess_env

logger = logging.getLogger(__name__)


_PR_URL_OWNER_REPO_RE = re.compile(
    r"github\.com/([^/]+)/([^/]+)/pull/\d+",
    re.IGNORECASE,
)


DEFAULT_GH_BIN: str = "gh"
DEFAULT_TIMEOUT: int = 60


class PRFeedbackRepoProtocol(Protocol):
    """Structural surface the poller needs from ``TaskRepository``."""

    async def list_open_pull_requests(self, plan_id: str) -> list[dict[str, Any]]: ...

    async def update_pull_request_state(self, pr_id: int, state: str) -> None: ...

    async def advance_pull_request_cursor(
        self,
        pr_id: int,
        *,
        last_seen_review_id: int | None,
        last_seen_check_run_id: int | None,
    ) -> None: ...

    async def emit_pr_event(
        self,
        event_type: str,
        *,
        plan_id: str | None,
        task_id: str | None,
        payload: dict[str, Any],
    ) -> int: ...


def _parse_owner_repo(pr_url: str) -> tuple[str, str] | None:
    if not pr_url:
        return None
    match = _PR_URL_OWNER_REPO_RE.search(pr_url)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _gh_view_argv(gh_bin: str, pr_number: int) -> list[str]:
    return [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--json",
        "reviewDecision,statusCheckRollup,latestReviews,reviewRequests,headRefOid,state",
    ]


def _gh_reviews_argv(gh_bin: str, owner: str, repo: str, pr_number: int) -> list[str]:
    return [gh_bin, "api", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews"]


def _gh_comments_argv(gh_bin: str, owner: str, repo: str, pr_number: int) -> list[str]:
    return [gh_bin, "api", f"repos/{owner}/{repo}/pulls/{pr_number}/comments"]


def _run_gh(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=gh_subprocess_env(),
        check=False,
    )


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _max_review_id(reviews: Iterable[Any]) -> int | None:
    ids: list[int] = []
    for entry in reviews:
        if not isinstance(entry, Mapping):
            continue
        rid = _coerce_int(entry.get("id"))
        if rid is not None:
            ids.append(rid)
    return max(ids) if ids else None


def _max_check_run_id(rollup: Iterable[Any]) -> int | None:
    ids: list[int] = []
    for node in rollup:
        if not isinstance(node, Mapping):
            continue
        for key in ("databaseId", "id"):
            cid = _coerce_int(node.get(key))
            if cid is not None:
                ids.append(cid)
                break
    return max(ids) if ids else None


def _latest_approved_login(latest_reviews: Iterable[Any]) -> str:
    for entry in latest_reviews:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("state") or "").upper() != "APPROVED":
            continue
        author = entry.get("author")
        if isinstance(author, Mapping):
            login = author.get("login")
            if isinstance(login, str) and login:
                return login
        user = entry.get("user")
        if isinstance(user, Mapping):
            login = user.get("login")
            if isinstance(login, str) and login:
                return login
    return ""


def _normalize_comments(comments: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in comments:
        if not isinstance(entry, Mapping):
            continue
        author = ""
        user = entry.get("user")
        if isinstance(user, Mapping):
            login = user.get("login")
            if isinstance(login, str):
                author = login
        out.append(
            {
                "body": entry.get("body", ""),
                "path": entry.get("path", ""),
                "line": entry.get("line"),
                "author": author,
            }
        )
    return out


async def poll_pr_feedback(
    repo: PRFeedbackRepoProtocol,
    plan_id: str,
    *,
    gh_bin: str = DEFAULT_GH_BIN,
    timeout: int = DEFAULT_TIMEOUT,
) -> int:
    """Poll every open PR row for ``plan_id`` once.

    Returns the number of rows that completed all three ``gh`` probes
    successfully. Rows whose probe sequence raised :class:`OSError` /
    :class:`subprocess.TimeoutExpired` or whose ``gh`` invocations
    exited non-zero count as failures and do not contribute to the
    return value (VAL-PR-013).
    """
    rows = await repo.list_open_pull_requests(plan_id)
    polled = 0
    for row in rows:
        if await _poll_one(repo, row, gh_bin=gh_bin, timeout=timeout):
            polled += 1
    return polled


async def _poll_one(
    repo: PRFeedbackRepoProtocol,
    row: Mapping[str, Any],
    *,
    gh_bin: str,
    timeout: int,
) -> bool:
    pr_number = int(row["pr_number"])
    pr_url = str(row["pr_url"])
    plan_id = str(row["plan_id"])
    task_id = str(row["task_id"])
    pr_id = int(row["id"])
    last_seen_review_id = int(row.get("last_seen_review_id") or 0)
    last_seen_check_run_id = int(row.get("last_seen_check_run_id") or 0)
    stored_head_sha = row.get("head_sha")

    parsed = _parse_owner_repo(pr_url)
    if parsed is None:
        logger.warning(
            "pr_feedback poller: PR #%s pr_url=%r — cannot parse owner/repo; skipping cycle without cursor advance",
            pr_number,
            pr_url,
        )
        return False
    owner, repo_name = parsed

    try:
        view_proc = _run_gh(_gh_view_argv(gh_bin, pr_number), timeout=timeout)
        if view_proc.returncode != 0:
            logger.warning(
                "pr_feedback poller: PR #%s — gh pr view exited %d (stderr=%r); cursor unchanged",
                pr_number,
                view_proc.returncode,
                (view_proc.stderr or "").strip(),
            )
            return False
        reviews_proc = _run_gh(
            _gh_reviews_argv(gh_bin, owner, repo_name, pr_number),
            timeout=timeout,
        )
        if reviews_proc.returncode != 0:
            logger.warning(
                "pr_feedback poller: PR #%s — gh api reviews exited %d (stderr=%r); cursor unchanged",
                pr_number,
                reviews_proc.returncode,
                (reviews_proc.stderr or "").strip(),
            )
            return False
        comments_proc = _run_gh(
            _gh_comments_argv(gh_bin, owner, repo_name, pr_number),
            timeout=timeout,
        )
        if comments_proc.returncode != 0:
            logger.warning(
                "pr_feedback poller: PR #%s — gh api comments exited %d (stderr=%r); cursor unchanged",
                pr_number,
                comments_proc.returncode,
                (comments_proc.stderr or "").strip(),
            )
            return False
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "pr_feedback poller: PR #%s — subprocess timed out (%s); cursor unchanged",
            pr_number,
            exc,
        )
        return False
    except OSError as exc:
        logger.warning(
            "pr_feedback poller: PR #%s — subprocess OSError (%s); cursor unchanged",
            pr_number,
            exc,
        )
        return False

    try:
        view_payload = json.loads(view_proc.stdout or "{}")
        reviews_payload = json.loads(reviews_proc.stdout or "[]")
        comments_payload = json.loads(comments_proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        logger.warning(
            "pr_feedback poller: PR #%s — JSON decode failed (%s); cursor unchanged",
            pr_number,
            exc,
        )
        return False

    if not isinstance(view_payload, Mapping):
        logger.warning(
            "pr_feedback poller: PR #%s — gh pr view returned non-object payload (%s); cursor unchanged",
            pr_number,
            type(view_payload).__name__,
        )
        return False

    review_decision = str(view_payload.get("reviewDecision") or "")
    pr_state = str(view_payload.get("state") or "").upper()
    head_sha = view_payload.get("headRefOid") or stored_head_sha or ""
    if not isinstance(head_sha, str):
        head_sha = str(head_sha)

    reviews_list: list[Any] = list(reviews_payload) if isinstance(reviews_payload, list) else []
    comments_list: list[Any] = list(comments_payload) if isinstance(comments_payload, list) else []
    rollup_raw = view_payload.get("statusCheckRollup")
    rollup_list: list[Any] = list(rollup_raw) if isinstance(rollup_raw, list) else []
    latest_reviews_raw = view_payload.get("latestReviews")
    latest_reviews_list: list[Any] = list(latest_reviews_raw) if isinstance(latest_reviews_raw, list) else []

    observed_review_id = _max_review_id(reviews_list)
    next_review_cursor = (
        max(observed_review_id, last_seen_review_id) if observed_review_id is not None else last_seen_review_id
    )
    has_new_reviews = observed_review_id is not None and observed_review_id > last_seen_review_id

    observed_check_run_id = _max_check_run_id(rollup_list)
    next_check_cursor = (
        max(observed_check_run_id, last_seen_check_run_id)
        if observed_check_run_id is not None
        else last_seen_check_run_id
    )

    if pr_state == "MERGED":
        merged_at = view_payload.get("mergedAt") or view_payload.get("merged_at")
        await repo.emit_pr_event(
            PR_MERGED_EVENT_TYPE,
            plan_id=plan_id,
            task_id=task_id,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_sha": head_sha,
                "merged_at": merged_at,
            },
        )
        await repo.update_pull_request_state(pr_id, "merged")
    elif has_new_reviews and review_decision == "APPROVED":
        await repo.emit_pr_event(
            PR_REVIEW_APPROVED_EVENT_TYPE,
            plan_id=plan_id,
            task_id=task_id,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_sha": head_sha,
                "reviewer": _latest_approved_login(latest_reviews_list),
            },
        )
    elif has_new_reviews and review_decision == "CHANGES_REQUESTED":
        await repo.emit_pr_event(
            PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
            plan_id=plan_id,
            task_id=task_id,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_sha": head_sha,
                "comments": _normalize_comments(comments_list),
            },
        )

    await repo.advance_pull_request_cursor(
        pr_id,
        last_seen_review_id=next_review_cursor or None,
        last_seen_check_run_id=next_check_cursor or None,
    )
    return True


__all__ = [
    "DEFAULT_GH_BIN",
    "DEFAULT_TIMEOUT",
    "PRFeedbackRepoProtocol",
    "poll_pr_feedback",
]
