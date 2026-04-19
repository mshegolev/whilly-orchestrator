"""GitHub Issues source adapter.

Reads open issues from a GitHub repo via the `gh` CLI and converts them into
whilly Tasks written to a `tasks.json` plan file.

Idempotent: re-fetching keeps the status of issues already taken into work.

CLI usage:
    whilly --source gh:owner/repo                 # default label whilly:ready
    whilly --source gh:owner/repo:ready           # custom label
    whilly --source gh:owner/repo --source-out my_plan.json

Programmatic:
    from whilly.sources import fetch_github_issues
    fetch_github_issues("owner/repo", label="whilly:ready", out_path="tasks.json")
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from whilly.task_manager import Task

log = logging.getLogger("whilly")


# ── Public spec ────────────────────────────────────────────────────────────────

DEFAULT_LABEL = "whilly:ready"
DEFAULT_LIMIT = 50
DEFAULT_GH_BIN = "gh"

# A small allowlist of priority labels we honor on issues.
PRIORITY_LABELS = {
    "priority:critical": "critical",
    "priority:high": "high",
    "priority:medium": "medium",
    "priority:low": "low",
}

# Heuristic regex to detect leaked secrets in issue body — best effort warning.
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),  # GitHub PAT
    re.compile(r"sk-[A-Za-z0-9]{32,}"),  # OpenAI / generic SK
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),  # Slack tokens
]


@dataclass
class FetchStats:
    """Summary of what changed during a fetch."""

    new: int = 0  # newly added tasks
    updated: int = 0  # mutable fields refreshed
    closed_externally: int = 0  # tasks marked skipped because issue was closed
    secret_warnings: list[str] = field(default_factory=list)
    total_open: int = 0  # total open issues retrieved


# ── CLI source spec parsing ────────────────────────────────────────────────────


@dataclass
class GitHubIssuesSource:
    """Parsed `--source gh:owner/repo[:label]` spec."""

    owner: str
    repo: str
    label: str = DEFAULT_LABEL
    limit: int = DEFAULT_LIMIT

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def project_name(self) -> str:
        return f"github-{self.owner}-{self.repo}"

    @classmethod
    def parse(cls, spec: str) -> GitHubIssuesSource:
        """Parse 'gh:owner/repo' or 'gh:owner/repo:label'.

        Accepts the leading 'gh:' prefix or a bare 'owner/repo' for convenience.
        """
        s = spec.strip()
        if s.startswith("gh:"):
            s = s[3:]

        # Split off optional label after a ':' that follows owner/repo.
        if s.count("/") < 1:
            raise ValueError(f"Invalid GitHub source spec: {spec!r} — expected gh:owner/repo[:label]")

        # owner/repo[:label]
        repo_part, sep, label = s.partition(":")
        owner_repo_pieces = repo_part.split("/", 1)
        if len(owner_repo_pieces) != 2 or not all(owner_repo_pieces):
            raise ValueError(f"Invalid GitHub source spec: {spec!r} — owner/repo missing")

        owner, repo = owner_repo_pieces
        return cls(owner=owner, repo=repo, label=(label or DEFAULT_LABEL))


# ── gh CLI invocation ──────────────────────────────────────────────────────────


def _gh_bin() -> str:
    """Resolve gh CLI path. WHILLY_GH_BIN env overrides for corp mirrors."""
    import os as _os

    return _os.environ.get("WHILLY_GH_BIN") or DEFAULT_GH_BIN


def _run_gh(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run `gh` with GITHUB_TOKEN unset to prefer the user's keyring auth.

    GITHUB_TOKEN env is sometimes set to expired CI tokens; gh prefers that env over the keyring,
    which causes spurious 401s. We unset it explicitly for this subprocess only.
    """
    import os as _os

    env = dict(_os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)

    cmd = [_gh_bin(), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False)


def _gh_issue_list(source: GitHubIssuesSource, timeout: int = 30) -> list[dict]:
    """Fetch open issues with the given label. Raises RuntimeError on failure."""
    args = [
        "issue",
        "list",
        "--repo",
        source.repo_full,
        "--state",
        "open",
        "--label",
        source.label,
        "--limit",
        str(source.limit),
        "--json",
        "number,title,body,labels,url,createdAt,updatedAt",
    ]
    proc = _run_gh(args, timeout=timeout)
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise RuntimeError(f"gh issue list failed (exit {proc.returncode}): {msg}")

    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned non-JSON: {exc}") from exc


# ── Issue → Task conversion ────────────────────────────────────────────────────


def _extract_section(body: str, section_name: str) -> list[str]:
    """Extract bullet items under a `## {section_name}` heading.

    Stops at the next heading or the end of the body. Returns text of bullet
    items with the leading dash/asterisk stripped.
    """
    if not body:
        return []

    lines = body.splitlines()
    in_section = False
    items: list[str] = []
    heading_re = re.compile(rf"^#{{1,6}}\s+{re.escape(section_name)}\b", re.IGNORECASE)
    next_heading_re = re.compile(r"^#{1,6}\s+\S")
    bullet_re = re.compile(r"^\s*[-*]\s+(.+?)\s*$")

    for ln in lines:
        if heading_re.match(ln):
            in_section = True
            continue
        if in_section and next_heading_re.match(ln):
            break
        if in_section:
            m = bullet_re.match(ln)
            if m:
                items.append(m.group(1).strip())
    return items


def _extract_inline_field(body: str, field_name: str) -> list[str]:
    """Extract '**Field:** a, b, c' style inline lists from issue body.

    Returns trimmed comma-separated tokens.
    """
    if not body:
        return []
    pattern = re.compile(rf"\*\*{re.escape(field_name)}:\*\*\s*(.+?)\s*(?:\n|$)", re.IGNORECASE)
    m = pattern.search(body)
    if not m:
        return []
    raw = m.group(1)
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _detect_secrets(text: str) -> list[str]:
    """Return names of secret pattern matches found in text. Best effort."""
    hits: list[str] = []
    for rx in _SECRET_PATTERNS:
        if rx.search(text):
            hits.append(rx.pattern)
    return hits


def _priority_from_labels(labels: list[dict]) -> str:
    """Map GitHub labels to whilly priority. Default 'medium'."""
    for label in labels:
        name = (label.get("name") or "").lower()
        if name in PRIORITY_LABELS:
            return PRIORITY_LABELS[name]
    return "medium"


def issue_to_task(issue: dict) -> tuple[Task, list[str]]:
    """Convert a single gh JSON issue into a whilly Task.

    Returns (task, secret_pattern_hits) — second element non-empty when the
    body matched any of the secret regexes (the caller can warn).
    """
    number = issue.get("number")
    title = issue.get("title", "").strip() or f"Issue #{number}"
    body = issue.get("body") or ""
    labels = issue.get("labels") or []
    url = issue.get("url", "")

    description_short = title
    if body:
        # Keep the first 480 chars of body so the task stays human-friendly.
        snippet = body.strip().replace("\r\n", "\n")
        if len(snippet) > 480:
            snippet = snippet[:480].rsplit("\n", 1)[0] + "\n…"
        description_short = f"{title}\n\n{snippet}"

    task = Task(
        id=f"GH-{number}",
        phase="GH-Issues",
        category="github-issue",
        priority=_priority_from_labels(labels),
        description=description_short,
        status="pending",
        dependencies=_extract_inline_field(body, "Depends"),
        key_files=_extract_inline_field(body, "Files"),
        acceptance_criteria=_extract_section(body, "Acceptance"),
        test_steps=_extract_section(body, "Test"),
        prd_requirement=url,
    )
    return task, _detect_secrets(body)


# ── Plan file IO + idempotent merge ────────────────────────────────────────────


def _build_source_block(source: GitHubIssuesSource) -> dict:
    return {
        "type": "github_issues",
        "repo": source.repo_full,
        "label": source.label,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


def _load_existing(plan_path: Path) -> dict:
    if not plan_path.exists():
        return {"project": "", "tasks": []}
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"existing plan {plan_path} is not valid JSON: {exc}") from exc


def _atomic_write_json(plan_path: Path, data: dict) -> None:
    """Write JSON atomically (tmp + os.replace)."""
    import os
    import tempfile

    dir_path = plan_path.parent
    dir_path.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".whilly_src_", suffix=".tmp")
    try:
        os.write(fd, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        os.close(fd)
        os.replace(tmp, plan_path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        if Path(tmp).exists():
            Path(tmp).unlink(missing_ok=True)
        raise


def merge_into_plan(
    issues: list[dict],
    source: GitHubIssuesSource,
    plan_path: Path,
) -> FetchStats:
    """Idempotent merge of fetched issues into the plan file at `plan_path`.

    - Adds new tasks (status pending).
    - For existing tasks (matched by id == "GH-{number}"): refreshes
      description, priority, key_files, acceptance, test_steps. Status preserved.
    - Tasks that previously came from this source but are no longer in `issues`
      and were still pending/in_progress are marked `skipped` (issue closed
      externally).
    """
    stats = FetchStats(total_open=len(issues))
    existing = _load_existing(plan_path)
    existing_tasks = existing.get("tasks", [])
    existing_by_id = {t.get("id"): t for t in existing_tasks}

    fetched_ids: set[str] = set()
    for raw in issues:
        task, secret_hits = issue_to_task(raw)
        fetched_ids.add(task.id)
        if secret_hits:
            stats.secret_warnings.append(f"{task.id}: matched patterns {secret_hits}")

        if task.id in existing_by_id:
            cur = existing_by_id[task.id]
            # Preserve status; refresh mutable fields.
            for field_name in (
                "description",
                "priority",
                "key_files",
                "acceptance_criteria",
                "test_steps",
                "prd_requirement",
                "dependencies",
            ):
                cur[field_name] = getattr(task, field_name)
            stats.updated += 1
        else:
            existing_tasks.append(task.to_dict())
            stats.new += 1

    # Mark stale tasks that disappeared from the source as skipped.
    for cur in existing_tasks:
        if not cur.get("id", "").startswith("GH-"):
            continue
        if cur["id"] in fetched_ids:
            continue
        if cur.get("status") in ("pending", "in_progress"):
            cur["status"] = "skipped"
            stats.closed_externally += 1

    # Stamp project + source block.
    if not existing.get("project"):
        existing["project"] = source.project_name
    existing["source"] = _build_source_block(source)
    existing["tasks"] = existing_tasks

    _atomic_write_json(plan_path, existing)
    return stats


# ── Top-level convenience function ─────────────────────────────────────────────


def fetch_github_issues(
    repo: str,
    label: str = DEFAULT_LABEL,
    out_path: str | Path = "tasks.json",
    limit: int = DEFAULT_LIMIT,
    timeout: int = 30,
) -> tuple[Path, FetchStats]:
    """Fetch open issues with the given label and merge into the plan file.

    Args:
        repo: 'owner/repo' string.
        label: GitHub label that gates inclusion (default whilly:ready).
        out_path: tasks.json path to upsert into.
        limit: max issues to fetch in one call.
        timeout: gh CLI timeout in seconds.

    Returns:
        (plan_path, stats) — the resolved Path and merge counters.
    """
    if "/" not in repo:
        raise ValueError(f"repo must be 'owner/repo', got {repo!r}")
    owner, repo_name = repo.split("/", 1)
    source = GitHubIssuesSource(owner=owner, repo=repo_name, label=label, limit=limit)

    issues = _gh_issue_list(source, timeout=timeout)
    plan_path = Path(out_path).resolve()
    stats = merge_into_plan(issues, source, plan_path)

    log.info(
        "GitHub source: %s issues fetched (new=%d, updated=%d, closed_externally=%d)",
        stats.total_open,
        stats.new,
        stats.updated,
        stats.closed_externally,
    )
    if stats.secret_warnings:
        for warning in stats.secret_warnings:
            log.warning("Secret-like pattern detected in issue body: %s", warning)

    return plan_path, stats
