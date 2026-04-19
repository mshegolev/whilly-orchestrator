#!/usr/bin/env python3
"""Whilly CI bot — runs whilly inside GitHub Actions on a schedule / label / manual trigger.

Pipeline:
    1. fetch_github_issues(repo, label) → tasks.json (idempotent, preserves status)
    2. Pick the first WHILLY_MAX_TASKS ready tasks
    3. Decision Gate on each → refuse → label flip + skip
    4. Run `whilly` headless with WHILLY_BUDGET_USD hard cap on the surviving tasks
    5. For each task that became `done` → open PR via GitHub PR sink
    6. Print a summary; exit 0 even on partial success (CI green)

Env vars:
    ANTHROPIC_API_KEY — required, used by Claude CLI inside whilly
    GH_TOKEN          — used by gh CLI (set automatically in Actions to GITHUB_TOKEN)
    WHILLY_REPO       — owner/repo (default: $GITHUB_REPOSITORY)
    WHILLY_LABEL      — label that gates issue inclusion (default: whilly:ready)
    WHILLY_BUDGET_USD — hard cap per CI run (default: 0.50)
    WHILLY_MAX_TASKS  — max tasks to attempt per run (default: 1)
    WHILLY_PLAN       — plan file path (default: whilly_ci_tasks.json)
    WHILLY_DRY_RUN    — '1' = fetch + decision gate only, no agent run, no PR

Exit codes:
    0  always (CI green) — partial failures logged but not propagated
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Make whilly importable when run directly from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whilly.decision_gate import (  # noqa: E402
    REFUSE,
    evaluate as decision_evaluate,
    label_flip_for_gh_task,
)
from whilly.sinks.github_pr import open_pr_for_task  # noqa: E402
from whilly.sources import fetch_github_issues  # noqa: E402
from whilly.task_manager import TaskManager  # noqa: E402

REPO = os.environ.get("WHILLY_REPO") or os.environ.get("GITHUB_REPOSITORY")
LABEL = os.environ.get("WHILLY_LABEL", "whilly:ready")
BUDGET_USD = os.environ.get("WHILLY_BUDGET_USD", "0.5")
MAX_TASKS = int(os.environ.get("WHILLY_MAX_TASKS", "1"))
PLAN_PATH = Path(os.environ.get("WHILLY_PLAN", "whilly_ci_tasks.json"))
DRY_RUN = os.environ.get("WHILLY_DRY_RUN") in ("1", "true", "yes")


def log(msg: str) -> None:
    print(f"[whilly-ci] {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n────── {title} ──────", flush=True)


def main() -> int:
    if not REPO:
        log("ERROR: WHILLY_REPO or GITHUB_REPOSITORY must be set")
        return 0  # CI green even on config error — fix in next run
    if not os.environ.get("ANTHROPIC_API_KEY") and not DRY_RUN:
        log("ERROR: ANTHROPIC_API_KEY not set; agent run will fail")
        return 0

    log(f"repo={REPO} label={LABEL} budget=${BUDGET_USD} max_tasks={MAX_TASKS} dry_run={DRY_RUN}")

    # ── 1) Fetch issues ────────────────────────────────────────────────────────
    section("1/5 fetch GitHub issues")
    try:
        plan_path, stats = fetch_github_issues(REPO, label=LABEL, out_path=PLAN_PATH)
    except Exception as exc:  # noqa: BLE001
        log(f"fetch failed: {exc}")
        return 0
    log(f"plan={plan_path} new={stats.new} updated={stats.updated} closed_externally={stats.closed_externally} total_open={stats.total_open}")
    if stats.secret_warnings:
        for w in stats.secret_warnings:
            log(f"WARN secret pattern: {w}")

    # ── 2) Pick ready tasks ────────────────────────────────────────────────────
    section("2/5 pick ready tasks")
    tm = TaskManager(plan_path)
    ready_all = tm.get_ready_tasks()
    if not ready_all:
        log("no ready tasks — nothing to do")
        return 0
    candidates = ready_all[:MAX_TASKS]
    log(f"{len(candidates)} candidate(s): {[t.id for t in candidates]}")

    # ── 3) Decision Gate ──────────────────────────────────────────────────────
    section("3/5 Decision Gate")
    survivors: list = []
    for task in candidates:
        if DRY_RUN:
            log(f"DRY_RUN: skip Decision Gate for {task.id}")
            survivors.append(task)
            continue
        d = decision_evaluate(task)
        log(f"{task.id}: {d.decision} ({d.reason}) cost=${d.cost_usd:.4f}")
        if d.decision == REFUSE:
            flipped = label_flip_for_gh_task(task, d)
            tm.mark_status([task.id], "skipped")
            log(f"  → status=skipped, label_flipped={flipped}")
        else:
            survivors.append(task)

    if not survivors:
        log("all candidates refused — nothing to run")
        return 0

    # ── 4) Run whilly headless on the surviving subset ────────────────────────
    section(f"4/5 run whilly headless on {[t.id for t in survivors]}")
    if DRY_RUN:
        log("DRY_RUN: skip agent run")
    else:
        # Trim plan to just survivors (and previously-completed tasks).
        keep = {t.id for t in survivors}
        tm.tasks = [t for t in tm.tasks if t.id in keep or t.status != "pending"]
        tm.save()

        env = os.environ.copy()
        env.update(
            {
                "WHILLY_BUDGET_USD": BUDGET_USD,
                "WHILLY_MAX_PARALLEL": "1",
                "WHILLY_MAX_ITERATIONS": "3",
                "WHILLY_USE_TMUX": "0",
                "WHILLY_USE_WORKSPACE": "0",
                "WHILLY_HEADLESS": "1",
            }
        )
        cmd = [sys.executable, "-m", "whilly", str(plan_path)]
        log(f"$ {' '.join(cmd)} (env: WHILLY_BUDGET_USD={BUDGET_USD}, WHILLY_USE_WORKSPACE=0)")
        try:
            proc = subprocess.run(cmd, env=env, timeout=1800, check=False)
            log(f"whilly exit code: {proc.returncode}")
        except subprocess.TimeoutExpired:
            log("whilly run timed out after 1800s")

    # ── 5) Open PRs for newly done tasks ──────────────────────────────────────
    section("5/5 open PRs for done tasks")
    tm.reload()
    surv_ids = {t.id for t in survivors}
    opened = 0
    for task in tm.tasks:
        if task.id not in surv_ids:
            continue
        if task.status != "done":
            log(f"{task.id} status={task.status} — skip PR")
            continue
        if DRY_RUN:
            log(f"DRY_RUN: would open PR for {task.id}")
            continue
        result = open_pr_for_task(task, worktree_path=Path.cwd())
        if result.ok:
            log(f"{task.id} → PR opened: {result.pr_url}")
            opened += 1
        else:
            log(f"{task.id} → PR FAILED: {result.reason}")

    log(f"\nDone. PRs opened: {opened}/{len(survivors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
