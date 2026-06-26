"""Convert scheduler-discovered Jira issues into claimable Whilly tasks.

The :class:`~whilly.scheduler.worker.SchedulerWorker` polls Jira and hands the
deduplicated issue dicts to an ``on_issues_found`` callback. Historically that
callback only logged; this module supplies the missing bridge: it maps each
JQL-matched issue onto the canonical v4 :class:`~whilly.core.models.Task` /
:class:`~whilly.core.models.Plan` so the existing ``whilly plan import`` insert
path (idempotent ``ON CONFLICT (id) DO NOTHING``) can persist them and any
worker — local or the opencode-devbox remote adapter — can claim them.

Everything here is **pure** (no I/O, no DB, no network): it builds value
objects. The actual INSERT lives in :func:`whilly.cli.plan._async_import`, which
the CLI layer drives so the scheduler subsystem stays free of a ``cli`` import.

Reuse over re-implementation: :func:`whilly.sources.jira.issue_to_task_dict`
already flattens Jira ADF descriptions, runs ``sanitize_external_text`` on
untrusted issue content, extracts ``## Acceptance`` / ``## Test`` bullets, and
maps the Jira priority — so we route every issue through it rather than parsing
fields a second (subtly different) way.
"""

from __future__ import annotations

import logging

from whilly.core.models import Plan, PlanOrigin, Priority, RepoTarget, Task, TaskStatus
from whilly.scheduler.models import SchedulerRule
from whilly.sources.jira import issue_to_task_dict

log = logging.getLogger(__name__)

# Jira priorities are already mapped to whilly's string buckets by
# ``issue_to_task_dict`` (via ``_jira_priority``); translate those to the
# typed enum, defaulting to MEDIUM for anything unrecognised.
_PRIORITY_BY_NAME: dict[str, Priority] = {
    "critical": Priority.CRITICAL,
    "high": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "low": Priority.LOW,
}

# Default clone-URL templates per provider when the rule does not pin one
# explicitly via ``custom_metadata.repo_clone_url``. GitLab defaults to the
# internal Acme host over SSH (matches how opencode-devbox clones repos).
_GITLAB_SSH_HOST = "gitlab.example.com"


def plan_id_for_rule(rule: SchedulerRule) -> str:
    """Return the plan id the rule's discovered issues are persisted under.

    A rule maps to exactly one plan so a worker pinned to a single
    ``WHILLY_PLAN_ID`` (the devbox adapter) drains all of the rule's issues.
    ``custom_metadata.plan_id`` overrides the default (the rule id) for
    operators who want the plan id to differ from the rule id.
    """
    explicit = str(rule.custom_metadata.get("plan_id") or "").strip()
    return explicit or rule.id


def resolve_repo_target(rule: SchedulerRule) -> RepoTarget | None:
    """Resolve an optional :class:`RepoTarget` from ``custom_metadata.repo_target``.

    The metadata value is ``"<provider>:<full_name>"`` (e.g.
    ``"gitlab:example-group/autotests/example-repo"`` or ``"github:org/repo"``). The clone
    URL is derived per provider unless ``custom_metadata.repo_clone_url`` pins
    one explicitly. Returns ``None`` when the metadata is absent or malformed
    (tasks then run repo-less in the worker's cwd).
    """
    raw = str(rule.custom_metadata.get("repo_target") or "").strip()
    if not raw or ":" not in raw:
        return None
    provider, _, full_name = raw.partition(":")
    provider = provider.strip().lower()
    full_name = full_name.strip()
    if not provider or not full_name:
        return None

    clone_url = str(rule.custom_metadata.get("repo_clone_url") or "").strip()
    if not clone_url:
        if provider == "github":
            clone_url = f"https://github.com/{full_name}.git"
        elif provider == "gitlab":
            clone_url = f"git@{_GITLAB_SSH_HOST}:{full_name}.git"

    return RepoTarget(
        id=raw,
        provider=provider,
        repo_full_name=full_name,
        clone_url=clone_url,
    )


def _task_from_issue(issue: dict, repo_target_id: str) -> Task | None:
    """Build a single PENDING :class:`Task` from a JQL-search issue dict.

    Returns ``None`` for an issue without a usable key so callers can skip it
    without aborting the batch.
    """
    key = str(issue.get("key") or "").strip()
    if not key:
        return None
    data = issue_to_task_dict(key, issue)
    priority = _PRIORITY_BY_NAME.get(str(data.get("priority") or "").lower(), Priority.MEDIUM)
    return Task(
        id=f"JIRA-{key}",
        status=TaskStatus.PENDING,
        priority=priority,
        description=str(data.get("description") or data.get("title") or key),
        acceptance_criteria=tuple(data.get("acceptance_criteria") or ()),
        test_steps=tuple(data.get("test_steps") or ()),
        prd_requirement=str(data.get("url") or ""),
        repo_target_id=repo_target_id,
    )


def build_plan_from_issues(rule: SchedulerRule, issues: list[dict]) -> Plan:
    """Build the one-plan-per-rule :class:`Plan` for a batch of discovered issues.

    Issues without a key are skipped. The resulting plan is safe to feed into
    the idempotent ``whilly plan import`` path repeatedly: pre-existing task
    rows hit ``ON CONFLICT (id) DO NOTHING`` and are left untouched.
    """
    repo_target = resolve_repo_target(rule)
    repo_target_id = repo_target.id if repo_target else ""

    tasks: list[Task] = []
    for issue in issues:
        task = _task_from_issue(issue, repo_target_id)
        if task is not None:
            tasks.append(task)

    plan_id = plan_id_for_rule(rule)
    origin = PlanOrigin(
        system="jira_scheduler",
        ref=rule.id,
        title=rule.name,
        decomposition_mode="scheduler_jql",
    )
    return Plan(
        id=plan_id,
        name=rule.name or plan_id,
        tasks=tuple(tasks),
        origin=origin,
        repo_targets=(repo_target,) if repo_target else (),
    )
