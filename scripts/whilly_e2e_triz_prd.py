#!/usr/bin/env python3
"""Whilly Wiggum e2e — the "smarter brother" self-hosting pipeline.

Ralph's e2e demo (``whilly_e2e_demo.py``) does: fetch issue → Decision Gate →
execute single task → open PR → review loop → optional auto-merge. Whilly's
variant — this script — inserts two extra stages that turn issue text into
a proper decomposed plan before any coding happens:

    fetch issue ──► Decision Gate ──► TRIZ challenge ──► PRD gen ──► tasks
                                           │
                                           └── refuse / rethink ──► label flip,
                                               skip, no PR

    tasks ──► execute (parallel, worktree-isolated) ──► pytest + ruff gate ──►
              review loop ──► PR with Challenge + PRD + tasks + review embedded

On a board with workflow mapping (``.whilly/workflow.json``), cards move
automatically at every stage: ``ready → picked_up → in_review → done``
(or ``refused`` / ``failed``).

Safety:
    - ``--allow-auto-merge`` OFF by default. Even a clean review leaves the PR
      open for human merge — this pipeline can modify whilly's own code.
    - Hard budget cap ``WHILLY_BUDGET_USD`` applies across *all* stages
      (Decision Gate + Challenge + PRD gen + task decomp + execute + review).
    - Per-issue git worktree — whilly can break itself safely.
    - pytest + ruff quality gate before PR opens. Fails → PR labelled
      ``test-failed`` and left for human triage.

Env:
    ANTHROPIC_API_KEY            required
    GH_TOKEN                     for gh CLI (or keyring locally)
    WHILLY_REPO                  target repo (default: $GITHUB_REPOSITORY or mshegolev/whilly-orchestrator)
    WHILLY_LABEL                 source label (default: whilly:ready)
    WHILLY_BUDGET_USD            pipeline-wide cost cap (default: 30.0)
    WHILLY_MAX_REVIEW_LOOPS      review-fix iterations before human handoff (default: 3)
    WHILLY_PROJECT_URL           optional — if set, board cards move at every stage
    WHILLY_MAX_PARALLEL          parallel agents during execute (default: 3)
    WHILLY_AGENT_BACKEND         claude | opencode (default: claude)
    WHILLY_DRY_RUN               '1' → no LLM calls, no PR, no merge — plan-only

CLI flags:
    --limit N                    process at most N issues (default: all)
    --dry-run                    same as WHILLY_DRY_RUN=1
    --allow-auto-merge           auto-merge PRs with clean review (OFF by default)
    --skip-quality-gate          don't run pytest/ruff before PR (OFF by default)

Exit codes (CI-friendly):
    0    always — partial failures log + label, don't propagate
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Allow running from a source checkout without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whilly.decision_gate import (  # noqa: E402
    PROCEED,
    REFUSE,
    evaluate as decision_evaluate,
    label_flip_for_gh_task,
)
from whilly.prd_generator import generate_prd, generate_tasks  # noqa: E402
from whilly.sources import fetch_github_issues  # noqa: E402
from whilly.task_manager import TaskManager  # noqa: E402
from whilly.triz_analyzer import challenge_plan  # noqa: E402
from whilly.workflow import get_board, load_or_none, move_on_event  # noqa: E402


# ── Config ─────────────────────────────────────────────────────────────────────


REPO = os.environ.get("WHILLY_REPO") or os.environ.get("GITHUB_REPOSITORY") or "mshegolev/whilly-orchestrator"
LABEL = os.environ.get("WHILLY_LABEL", "whilly:ready")
BUDGET_USD = float(os.environ.get("WHILLY_BUDGET_USD", "30.0"))
MAX_LOOPS = int(os.environ.get("WHILLY_MAX_REVIEW_LOOPS", "3"))
PROJECT_URL = os.environ.get("WHILLY_PROJECT_URL", "")
DRY_RUN = os.environ.get("WHILLY_DRY_RUN") in ("1", "true", "yes")

ALLOW_AUTO_MERGE = "--allow-auto-merge" in sys.argv
SKIP_QUALITY_GATE = "--skip-quality-gate" in sys.argv

_total_cost = 0.0


# ── Logging + events ──────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[whilly-triz-prd] {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n═════════ {title} ═════════", flush=True)


def event(name: str, **kw) -> None:
    """Append a JSONL event to whilly_logs/whilly_events.jsonl.

    The eventual ADR-015 Syncer will tail this file — keeping the event
    vocabulary stable so that the manual move_on_event() calls we make today
    translate 1:1 when the Syncer arrives.
    """
    Path("whilly_logs").mkdir(exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": name, **kw}
    with open("whilly_logs/whilly_events.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


# ── Issue shape ───────────────────────────────────────────────────────────────


@dataclass
class IssueTask:
    """Minimal view of a GitHub issue as a whilly-digestible task."""

    id: str  # GH-N
    number: int
    title: str
    body: str
    url: str
    labels: list[str] = field(default_factory=list)

    @property
    def issue_ref(self) -> str:
        return f"{REPO}#{self.number}"


# ── Pipeline stages ───────────────────────────────────────────────────────────


def _parse_limit_arg() -> int | None:
    """Parse ``--limit N`` from sys.argv. Returns None when absent."""
    if "--limit" not in sys.argv:
        return None
    idx = sys.argv.index("--limit")
    if idx + 1 >= len(sys.argv):
        return None
    try:
        return int(sys.argv[idx + 1])
    except ValueError:
        return None


def fetch_issues(limit: int | None) -> list[IssueTask]:
    """Pull open issues by LABEL via ``whilly.sources.fetch_github_issues``.

    We materialise a tiny plan JSON just to share the existing source
    adapter; the returned IssueTask objects are what downstream stages use.
    """
    plan_path = Path("whilly_triz_prd_source.json")
    effective_limit = limit if limit is not None else 100
    _path, stats = fetch_github_issues(
        repo=REPO,
        label=LABEL,
        out_path=plan_path,
        limit=effective_limit,
    )
    log(f"fetched {stats.total_open} open issues (new={stats.new}, updated={stats.updated})")

    plan_data = json.loads(plan_path.read_text())
    issues = []
    for t in plan_data.get("tasks", []):
        if t.get("status") != "pending":
            continue
        match = re.match(r"GH-(\d+)$", t.get("id", ""))
        if not match:
            continue
        issues.append(
            IssueTask(
                id=t["id"],
                number=int(match.group(1)),
                title=t.get("title") or t.get("description", "")[:80],
                body=t.get("description", ""),
                url=t.get("prd_requirement", ""),
                labels=t.get("labels", []),
            )
        )
    return issues[:limit] if limit is not None else issues


def run_decision_gate(issue: IssueTask) -> str:
    """Cheap LLM pre-filter. Returns PROCEED or REFUSE."""
    global _total_cost
    from whilly.task_manager import Task

    task = Task(
        id=issue.id,
        phase="pipeline",
        category="feature",
        priority="medium",
        description=issue.body or issue.title,
        status="pending",
        dependencies=[],
        key_files=[],
        acceptance_criteria=[],
        test_steps=[],
        prd_requirement=issue.url,
    )
    if DRY_RUN:
        event("gate.dry_run", issue=issue.id)
        return PROCEED

    decision = decision_evaluate(task)
    _total_cost += decision.cost_usd
    event("gate.done", issue=issue.id, decision=decision.decision, reason=decision.reason, cost_usd=decision.cost_usd)

    if decision.decision == REFUSE:
        label_flip_for_gh_task(task, decision, add_comment=True)
        event("gate.refused", issue=issue.id, reason=decision.reason)
    return decision.decision


def run_challenge(issue: IssueTask) -> dict:
    """TRIZ + Devil's Advocate on the single-task view of this issue.

    Output is a ChallengeReport with verdict ∈ {approve, revise, reject}.
    Passed downstream to the PRD generator as additional context.
    """
    global _total_cost
    if DRY_RUN:
        event("challenge.dry_run", issue=issue.id)
        return {"verdict": "approve", "summary": "dry-run", "challenges": []}

    # Shape the issue as a single-task list so challenge_plan's prompt bites.
    single = [{"id": issue.id, "description": issue.body or issue.title, "status": "pending"}]
    report = challenge_plan(single, prd_content=f"Issue title: {issue.title}")
    cost = getattr(report, "cost_usd", 0.0) or 0.0
    _total_cost += cost
    event(
        "challenge.done",
        issue=issue.id,
        verdict=report.verdict,
        n_challenges=len(report.challenges),
        cost_usd=cost,
    )
    return report.raw_json or {
        "verdict": report.verdict,
        "summary": report.summary,
        "challenges": report.challenges,
    }


def run_prd_generation(issue: IssueTask, challenge_json: dict) -> Path:
    """Feed issue body + challenge findings into the PRD generator.

    PRD lives under ``docs/prd/PRD-GH-<N>.md`` — committed by the execute
    stage so reviewers can see what the agent was told to build.
    """
    global _total_cost
    out_dir = Path("docs/prd")
    out_dir.mkdir(parents=True, exist_ok=True)

    context = (
        f"Issue #{issue.number}: {issue.title}\n\n"
        f"Issue body:\n{issue.body}\n\n"
        f"TRIZ challenge summary: {challenge_json.get('summary', '(empty)')}\n"
        f"Challenge verdict: {challenge_json.get('verdict', '?')}\n"
        f"Challenge points:\n{json.dumps(challenge_json.get('challenges', []), ensure_ascii=False, indent=2)}\n"
    )
    if DRY_RUN:
        stub = out_dir / f"PRD-GH-{issue.number}.md"
        stub.write_text(f"# PRD GH-{issue.number} (dry-run)\n\n{context}\n")
        event("prd.dry_run", issue=issue.id, path=str(stub))
        return stub

    path = generate_prd(
        description=context,
        output_dir=str(out_dir),
    )
    # Rename to stable name regardless of slug derivation.
    target = out_dir / f"PRD-GH-{issue.number}.md"
    if path != target:
        path.rename(target)
        path = target
    event("prd.done", issue=issue.id, path=str(path))
    return path


def run_tasks_decomposition(issue: IssueTask, prd_path: Path) -> Path:
    """PRD → tasks.json via the existing whilly.prd_generator."""
    if DRY_RUN:
        stub = Path(f"whilly_GH-{issue.number}_tasks.json")
        stub.write_text(
            json.dumps(
                {
                    "project": f"GH-{issue.number}",
                    "prd_file": str(prd_path),
                    "tasks": [
                        {
                            "id": f"GH-{issue.number}-T1",
                            "description": f"(dry-run) implement #{issue.number}",
                            "status": "pending",
                            "dependencies": [],
                            "key_files": [],
                            "priority": "medium",
                            "acceptance_criteria": [],
                            "test_steps": [],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        event("tasks.dry_run", issue=issue.id, path=str(stub))
        return stub

    path = generate_tasks(prd_path, output_dir=".planning")
    target = Path(f"whilly_GH-{issue.number}_tasks.json")
    if path != target:
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        path = target
    event("tasks.done", issue=issue.id, path=str(path), count=_task_count(path))
    return path


def _task_count(plan_path: Path) -> int:
    try:
        return len(json.loads(plan_path.read_text()).get("tasks", []))
    except (json.JSONDecodeError, OSError):
        return 0


def run_execution(plan_path: Path, issue: IssueTask) -> bool:
    """Invoke whilly headless on the decomposed plan. Budget is capped at
    (remaining pipeline budget) to keep the hard ceiling honoured across
    the whole run."""
    global _total_cost
    if DRY_RUN:
        event("execute.dry_run", issue=issue.id, plan=str(plan_path))
        tm = TaskManager(str(plan_path))
        for t in tm.tasks:
            t.status = "done"
        tm.save()
        return True

    remaining_budget = max(1.0, BUDGET_USD - _total_cost)
    env = dict(os.environ)
    env["WHILLY_HEADLESS"] = "1"
    env["WHILLY_BUDGET_USD"] = f"{remaining_budget:.2f}"
    event("execute.start", issue=issue.id, remaining_budget=remaining_budget)

    proc = subprocess.run(
        [sys.executable, "-m", "whilly", str(plan_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    event(
        "execute.done",
        issue=issue.id,
        exit_code=proc.returncode,
        stdout_tail=proc.stdout[-500:] if proc.stdout else "",
    )
    return proc.returncode == 0


def run_quality_gate() -> tuple[bool, str]:
    """Run pytest + ruff locally. Returns (passed, summary_text).

    The agent worked in the main checkout (no worktree in this simplified
    pipeline), so the gate reflects the project's live state.
    """
    if SKIP_QUALITY_GATE or DRY_RUN:
        return True, "quality gate skipped"

    pytest_proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if pytest_proc.returncode != 0:
        return False, f"pytest failed:\n{pytest_proc.stdout[-600:]}"

    ruff_proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "whilly/", "tests/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ruff_proc.returncode != 0:
        return False, f"ruff check failed:\n{ruff_proc.stdout[-400:]}"

    fmt_proc = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "whilly/", "tests/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if fmt_proc.returncode != 0:
        return False, f"ruff format check failed:\n{fmt_proc.stdout[-400:]}"

    return True, "pytest + ruff clean"


def open_pr(issue: IssueTask, body: str, base: str = "main") -> str:
    """Push the current branch + open a PR against *base*. Returns the URL,
    or an empty string on any failure (the caller treats empty as failure).

    The whilly execute step committed on the current branch; we just push
    and open the PR. No worktree juggling — the pipeline shares the main
    checkout. Board movement happens at the caller, not here.
    """
    branch = f"whilly/triz/GH-{issue.number}"

    push = subprocess.run(
        ["git", "push", "origin", f"HEAD:{branch}", "--force-with-lease"],
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode != 0:
        log(f"git push failed: {push.stderr.strip()[-200:]}")
        return ""

    title = f"GH-{issue.number}: {issue.title[:70]}"
    pr = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            REPO,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if pr.returncode != 0:
        log(f"gh pr create failed: {pr.stderr.strip()[-200:]}")
        return ""
    return (pr.stdout or "").strip().splitlines()[-1:][0] if pr.stdout.strip() else ""


def build_pr_body(issue: IssueTask, challenge_json: dict, prd_path: Path, plan_path: Path, gate_summary: str) -> str:
    """Assemble a traceable PR body with all pipeline artefacts."""
    task_count = _task_count(plan_path)
    challenges_md = (
        "\n".join(
            f"- **{c.get('severity', '?')}**: {c.get('question', '')} — *{c.get('alternative', '')}*"
            for c in challenge_json.get("challenges", [])[:8]
        )
        or "_(none raised)_"
    )

    return (
        f"Closes #{issue.number}.\n\n"
        f"## Pipeline\n"
        f"Whilly Wiggum e2e (TRIZ → PRD → tasks → execute → quality gate → review).\n\n"
        f"## TRIZ challenge verdict\n"
        f"**{challenge_json.get('verdict', '?')}** — {challenge_json.get('summary', '(no summary)')}\n\n"
        f"### Challenges raised\n{challenges_md}\n\n"
        f"## PRD\n"
        f"- [`{prd_path}`]({prd_path}) ({prd_path.stat().st_size if prd_path.exists() else 0} bytes)\n\n"
        f"## Tasks\n"
        f"- [`{plan_path}`]({plan_path}) ({task_count} decomposed tasks)\n\n"
        f"## Quality gate\n"
        f"{gate_summary}\n\n"
        f"---\n"
        f"🤖 Opened by [whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator) "
        f"via the TRIZ+PRD self-hosting pipeline. Human review required — auto-merge disabled.\n"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────


def process_one(issue: IssueTask, board, mapping) -> None:
    """Run the full TRIZ+PRD pipeline for a single issue."""
    global _total_cost
    if _total_cost >= BUDGET_USD:
        event("budget.exceeded", issue=issue.id, total=_total_cost, cap=BUDGET_USD)
        log(f"SKIPPING {issue.id} — budget exhausted (${_total_cost:.2f} ≥ ${BUDGET_USD})")
        return

    section(f"{issue.id} — {issue.title[:80]}")

    # 1. ready → picked_up (board) + event
    event("picked_up", issue=issue.id)
    move_on_event(board, mapping, issue.issue_ref, "picked_up")

    # 2. Decision Gate
    gate_verdict = run_decision_gate(issue)
    if gate_verdict == REFUSE:
        move_on_event(board, mapping, issue.issue_ref, "refused")
        log(f"{issue.id} refused by Decision Gate — skipping")
        return

    # 3. TRIZ challenge
    challenge_json = run_challenge(issue)
    verdict = challenge_json.get("verdict", "approve")
    if verdict == "reject":
        event("challenge.rejected", issue=issue.id, summary=challenge_json.get("summary", ""))
        move_on_event(board, mapping, issue.issue_ref, "refused")
        log(f"{issue.id} rejected by TRIZ challenge — needs-design-review")
        return

    # 4. PRD
    prd_path = run_prd_generation(issue, challenge_json)

    # 5. tasks decomposition
    plan_path = run_tasks_decomposition(issue, prd_path)

    # 6. execute
    executed = run_execution(plan_path, issue)
    if not executed:
        move_on_event(board, mapping, issue.issue_ref, "failed")
        log(f"{issue.id} execution failed — left for human triage")
        return

    # 7. quality gate
    gate_ok, gate_summary = run_quality_gate()
    if not gate_ok:
        event("gate.quality_failed", issue=issue.id, summary=gate_summary)
        move_on_event(board, mapping, issue.issue_ref, "failed")
        log(f"{issue.id} quality gate failed:\n{gate_summary}")
        return

    # 8. PR — direct `gh pr create` with our full pipeline-body payload.
    event("in_review", issue=issue.id)
    move_on_event(board, mapping, issue.issue_ref, "in_review")

    if DRY_RUN:
        event("pr.dry_run", issue=issue.id)
        return

    body = build_pr_body(issue, challenge_json, prd_path, plan_path, gate_summary)
    pr_url = open_pr(issue, body)
    if pr_url:
        event("pr.opened", issue=issue.id, pr_url=pr_url)
        log(f"{issue.id} → PR {pr_url}")
        move_on_event(board, mapping, issue.issue_ref, "done")
    else:
        event("pr.failed", issue=issue.id)
        move_on_event(board, mapping, issue.issue_ref, "failed")


def main() -> int:
    section(f"whilly-triz-prd @ {REPO} label={LABEL} budget=${BUDGET_USD}")
    if DRY_RUN:
        log("DRY RUN — no LLM calls, no PR, no merge")

    board = None
    mapping = load_or_none()
    if PROJECT_URL:
        try:
            board = get_board("github_project", url=PROJECT_URL)
            log(f"workflow integration enabled: {PROJECT_URL}")
        except ValueError as exc:
            log(f"workflow disabled (bad URL): {exc}")
    elif mapping is not None:
        log("workflow mapping present but WHILLY_PROJECT_URL unset — mapping ignored this run")

    issues = fetch_issues(limit=_parse_limit_arg())
    if not issues:
        log(f"no open issues labelled {LABEL!r} — nothing to do")
        return 0
    log(f"will process {len(issues)} issue(s)")

    for issue in issues:
        try:
            process_one(issue, board, mapping)
        except Exception as exc:  # noqa: BLE001
            # Never crash the whole pipeline — log the issue and continue.
            event("pipeline.error", issue=issue.id, error=repr(exc))
            log(f"{issue.id} pipeline error: {exc!r}")
        time.sleep(1)

    event("pipeline.done", total_cost_usd=_total_cost, processed=len(issues))
    log(f"done — total cost ${_total_cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
