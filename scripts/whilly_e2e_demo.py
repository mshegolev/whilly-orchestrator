#!/usr/bin/env python3
"""End-to-end demo: whilly picks a GitHub issue, writes the code, opens a PR,
reviews it, fixes review comments, and (optionally) auto-merges when clean.

Pipeline (one issue, up to 3 review-fix iterations):

    1. fetch_github_issues(REPO, label=whilly:ready, limit=1) → tasks.json
    2. Decision Gate: refuse → label flip + exit 0
    3. Create branch whilly/GH-N + per-task worktree
    4. Run whilly agent on the single task (headless, hard budget cap)
    5. push branch → gh pr create (opens PR)
    6. Reviewer agent reads `gh pr diff` and emits structured review JSON
       {clean: bool, comments: [{file, line, severity, message}]}
    7a. If clean: gh pr merge --squash --delete-branch (only with --allow-auto-merge)
    7b. If comments: gh pr review --comment with the rendered review,
        then run a "fixer" agent with original task + comments as context,
        commit + push, go back to step 6 (max 3 review-fix iterations).
    8. After max iterations: post a final comment, leave PR open for human review.

Safety:
    - --allow-auto-merge OFF by default. Without it, even a clean review
      leaves the PR open for human merge — matches BRD §10 D9.
    - Hard budget cap WHILLY_BUDGET_USD applies across the whole demo.
    - Reviewer + fixer cost is tracked alongside main agent cost in the
      JSONL events (events: review.start, review.done, fix.start, fix.done).

Env:
    ANTHROPIC_API_KEY   required
    GH_TOKEN            for gh CLI (auto in Actions; locally use gh keyring)
    WHILLY_REPO         default: $GITHUB_REPOSITORY or mshegolev/whilly-orchestrator
    WHILLY_LABEL        default: whilly:ready
    WHILLY_BUDGET_USD   default: 5.0  (this demo intentionally allows more
                                       than CI bot's 0.50 — it's multi-step)
    WHILLY_MAX_REVIEW_LOOPS  default: 3
    WHILLY_DRY_RUN      "1" → fetch + planning only, no LLM, no PR, no merge

Exit codes:
    0  always  — even partial failures log but do not propagate (CI-friendly)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Make whilly importable when run from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whilly.agents import get_backend  # noqa: E402
from whilly.decision_gate import (  # noqa: E402
    REFUSE,
    evaluate as decision_evaluate,
    label_flip_for_gh_task,
)
from whilly.sinks.github_pr import open_pr_for_task  # noqa: E402
from whilly.sources import fetch_github_issues  # noqa: E402
from whilly.task_manager import Task, TaskManager  # noqa: E402

# ── Config ─────────────────────────────────────────────────────────────────────

REPO = os.environ.get("WHILLY_REPO") or os.environ.get("GITHUB_REPOSITORY") or "mshegolev/whilly-orchestrator"
LABEL = os.environ.get("WHILLY_LABEL", "whilly:ready")
BUDGET_USD = float(os.environ.get("WHILLY_BUDGET_USD", "5.0"))
MAX_LOOPS = int(os.environ.get("WHILLY_MAX_REVIEW_LOOPS", "3"))
PLAN_PATH = Path(os.environ.get("WHILLY_PLAN", "whilly_e2e_tasks.json"))
DRY_RUN = os.environ.get("WHILLY_DRY_RUN") in ("1", "true", "yes")
AGENT_BACKEND = os.environ.get("WHILLY_AGENT_BACKEND", "claude")

# CLI flags
ALLOW_AUTO_MERGE = "--allow-auto-merge" in sys.argv

# Cost accounting (single budget across all phases of this demo).
_total_cost = 0.0


# ── Output helpers ─────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[whilly-e2e] {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n────── {title} ──────", flush=True)


def event(name: str, **kw) -> None:
    """Append a JSONL event to whilly_logs/whilly_events.jsonl."""
    Path("whilly_logs").mkdir(exist_ok=True)
    with open("whilly_logs/whilly_events.jsonl", "a") as f:
        from datetime import datetime, timezone

        f.write(
            json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), "event": name, **kw},
                ensure_ascii=False,
            )
            + "\n"
        )


# ── gh wrappers (token-clean) ──────────────────────────────────────────────────


def _strip_env_tokens() -> dict:
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return env


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=_strip_env_tokens(), check=False, **kw)


def gh_pr_diff(pr_url_or_number: str) -> str:
    proc = _run(["gh", "pr", "diff", pr_url_or_number, "--repo", REPO])
    if proc.returncode != 0:
        log(f"gh pr diff failed: {proc.stderr.strip()}")
        return ""
    return proc.stdout or ""


def gh_pr_view(pr_url_or_number: str) -> dict:
    proc = _run([
        "gh",
        "pr",
        "view",
        pr_url_or_number,
        "--repo",
        REPO,
        "--json",
        "number,url,headRefName,mergeable,reviewDecision",
    ])
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def gh_pr_review_comment(pr_number: str, body: str) -> bool:
    """Post a single PR review comment (request changes)."""
    proc = _run([
        "gh",
        "pr",
        "review",
        pr_number,
        "--repo",
        REPO,
        "--request-changes",
        "--body",
        body,
    ])
    if proc.returncode != 0:
        log(f"gh pr review failed: {proc.stderr.strip()}")
        return False
    return True


def gh_pr_approve(pr_number: str, body: str = "Whilly review: clean. Approving.") -> bool:
    proc = _run([
        "gh",
        "pr",
        "review",
        pr_number,
        "--repo",
        REPO,
        "--approve",
        "--body",
        body,
    ])
    return proc.returncode == 0


def gh_pr_merge(pr_number: str) -> bool:
    proc = _run([
        "gh",
        "pr",
        "merge",
        pr_number,
        "--repo",
        REPO,
        "--squash",
        "--delete-branch",
    ])
    if proc.returncode != 0:
        log(f"gh pr merge failed: {proc.stderr.strip()}")
        return False
    return True


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=str(cwd) if cwd else None)


# ── Reviewer agent ─────────────────────────────────────────────────────────────


REVIEWER_PROMPT = """Ты — строгий, но конструктивный code reviewer для Python-проекта whilly-orchestrator.

Тебе дан diff PR, который реализует следующую задачу:

----- TASK -----
ID: {task_id}
Description:
{description}

Acceptance criteria:
{acceptance}

Test steps:
{tests}
----- END TASK -----

----- DIFF -----
{diff}
----- END DIFF -----

Сделай review. Найди:
- ошибки логики / баги
- нарушения acceptance criteria
- проблемы безопасности
- грубые style violations (длинные функции, мёртвый код)
- отсутствие тестов когда они уместны

Игнорируй nit-picks (формат комментариев, имена переменных и т.п.).

Ответ строго одной строкой валидного JSON, без пояснений и без блока ```:
{{"clean": true|false, "summary": "≤200 chars", "comments": [{{"file": "path", "severity": "blocker|major|minor", "message": "≤300 chars"}}]}}

Если diff полностью соответствует задаче и не содержит проблем — clean=true, comments=[].
"""


@dataclass
class ReviewComment:
    file: str
    severity: str
    message: str


@dataclass
class ReviewResult:
    clean: bool
    summary: str
    comments: list[ReviewComment] = field(default_factory=list)
    cost_usd: float = 0.0
    raw_text: str = ""


def review_pr(pr_number: str, task: Task) -> ReviewResult:
    """Run a Claude/OpenCode agent in reviewer mode against the PR diff."""
    global _total_cost

    diff = gh_pr_diff(pr_number)
    if not diff:
        return ReviewResult(clean=False, summary="empty diff — cannot review", cost_usd=0.0)

    if len(diff) > 60_000:
        log(f"diff is large ({len(diff)} chars), truncating to 60k for reviewer prompt")
        diff = diff[:60_000] + "\n... [truncated]"

    prompt = REVIEWER_PROMPT.format(
        task_id=task.id,
        description=task.description,
        acceptance="\n".join(f"- {a}" for a in task.acceptance_criteria) or "(none)",
        tests="\n".join(f"- {t}" for t in task.test_steps) or "(none)",
        diff=diff,
    )

    backend = get_backend(AGENT_BACKEND)
    log(f"reviewer agent (backend={AGENT_BACKEND}) running on diff ({len(diff)} chars)")
    event("review.start", task_id=task.id, pr=pr_number, diff_chars=len(diff))

    if DRY_RUN:
        return ReviewResult(clean=True, summary="DRY_RUN: no review performed", cost_usd=0.0)

    result = backend.run(prompt, timeout=180)
    _total_cost += result.usage.cost_usd
    event(
        "review.done",
        task_id=task.id,
        pr=pr_number,
        cost_usd=result.usage.cost_usd,
        duration_s=result.duration_s,
        running_total=_total_cost,
    )

    parsed = _parse_review(result.result_text)
    parsed.cost_usd = result.usage.cost_usd
    parsed.raw_text = result.result_text
    return parsed


def _parse_review(raw: str) -> ReviewResult:
    """Defensive parse of the reviewer JSON. Fail-open to clean=False."""
    if not raw:
        return ReviewResult(clean=False, summary="empty reviewer output")

    # Try direct json
    candidate = raw.strip()
    parsed = None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Try to extract embedded JSON
        m = re.search(r'\{[^{}]*"clean"\s*:\s*(?:true|false)[\s\S]*?\}', candidate)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(parsed, dict):
        return ReviewResult(clean=False, summary="reviewer output not parseable", raw_text=raw[:200])

    clean = bool(parsed.get("clean", False))
    summary = str(parsed.get("summary", ""))[:200]
    comments_raw = parsed.get("comments") or []
    comments = []
    for c in comments_raw if isinstance(comments_raw, list) else []:
        if not isinstance(c, dict):
            continue
        comments.append(
            ReviewComment(
                file=str(c.get("file", "")),
                severity=str(c.get("severity", "minor")),
                message=str(c.get("message", ""))[:300],
            )
        )

    # Consistency: clean=True should mean zero blocker/major comments.
    if clean and any(c.severity in ("blocker", "major") for c in comments):
        clean = False
        summary = (summary + " [inconsistent: clean=true with blocker comments]").strip()

    return ReviewResult(clean=clean, summary=summary, comments=comments)


def render_review_body(review: ReviewResult, iteration: int) -> str:
    lines = [
        f"### Whilly review (iteration {iteration})",
        "",
        f"**Summary:** {review.summary or '(no summary)'}",
        "",
    ]
    if review.comments:
        lines.append("**Comments:**")
        for c in review.comments:
            lines.append(f"- `[{c.severity}]` `{c.file}` — {c.message}")
    else:
        lines.append("No comments.")
    lines.extend(
        [
            "",
            "---",
            f"🤖 Generated by `scripts/whilly_e2e_demo.py` (backend: `{AGENT_BACKEND}`, cost ${review.cost_usd:.4f})",
        ]
    )
    return "\n".join(lines)


# ── Fixer agent ────────────────────────────────────────────────────────────────


FIXER_PROMPT = """Ты получил review с замечаниями к своему PR. Исправь их.

----- ORIGINAL TASK -----
ID: {task_id}
{description}

Acceptance criteria:
{acceptance}
----- END TASK -----

----- REVIEW COMMENTS -----
{comments}
----- END REVIEW COMMENTS -----

Действуй:
1. Прочитай текущее состояние файлов проекта.
2. Применяй правки точечно: каждое замечание должно быть либо исправлено, либо явно опровергнуто (с обоснованием в commit message).
3. Не добавляй новых features, которых не было в task.
4. Когда закончишь — выведи строку <promise>COMPLETE</promise>.

После твоих изменений будет повторный review (всего до {max_loops} итераций).
"""


def run_fixer(task: Task, review: ReviewResult, iteration: int, branch: str) -> bool:
    """Run an agent in fix mode. Returns True on success."""
    global _total_cost

    comments_block = "\n".join(
        f"- [{c.severity}] {c.file}: {c.message}" for c in review.comments
    ) or "(no specific comments — re-read summary above)"

    prompt = FIXER_PROMPT.format(
        task_id=task.id,
        description=task.description,
        acceptance="\n".join(f"- {a}" for a in task.acceptance_criteria) or "(none)",
        comments=comments_block,
        max_loops=MAX_LOOPS,
    )

    log(f"fixer agent (iteration {iteration}, backend={AGENT_BACKEND}) addressing {len(review.comments)} comment(s)")
    event("fix.start", task_id=task.id, iteration=iteration, comment_count=len(review.comments))

    if DRY_RUN:
        log("DRY_RUN: skip fixer LLM call")
        return True

    backend = get_backend(AGENT_BACKEND)
    result = backend.run(prompt, timeout=900)
    _total_cost += result.usage.cost_usd
    event(
        "fix.done",
        task_id=task.id,
        iteration=iteration,
        cost_usd=result.usage.cost_usd,
        duration_s=result.duration_s,
        is_complete=result.is_complete,
        running_total=_total_cost,
    )

    if not result.is_complete:
        log(f"fixer did not signal complete (cost ${result.usage.cost_usd:.4f}); pushing whatever changed anyway")

    # Commit any modifications + push.
    add_proc = _git("add", "-A", cwd=Path.cwd())
    if add_proc.returncode != 0:
        log(f"git add failed: {add_proc.stderr}")
        return False

    diff_check = _git("diff", "--cached", "--quiet", cwd=Path.cwd())
    if diff_check.returncode == 0:
        log("fixer made no changes — skipping commit")
        return True

    commit_proc = _git(
        "commit",
        "-m",
        f"fix({task.id}): address review iteration {iteration}",
        cwd=Path.cwd(),
    )
    if commit_proc.returncode != 0:
        log(f"git commit failed: {commit_proc.stderr}")
        return False

    push_proc = _git("push", "origin", f"HEAD:{branch}", "--force-with-lease", cwd=Path.cwd())
    if push_proc.returncode != 0:
        log(f"git push failed: {push_proc.stderr}")
        return False

    return True


# ── Main agent (initial implementation) ────────────────────────────────────────


def run_main_agent(task: Task) -> bool:
    """Run the initial implementation agent via `python -m whilly` (reuses the loop)."""
    global _total_cost

    if DRY_RUN:
        log("DRY_RUN: skip main agent run")
        return True

    # Trim plan to a single-task plan so the loop only does this work.
    tm = TaskManager(PLAN_PATH)
    tm.tasks = [t for t in tm.tasks if t.id == task.id or t.status != "pending"]
    tm.save()

    env = os.environ.copy()
    env.update(
        {
            "WHILLY_BUDGET_USD": str(BUDGET_USD),
            "WHILLY_MAX_PARALLEL": "1",
            "WHILLY_MAX_ITERATIONS": "3",
            "WHILLY_USE_TMUX": "0",
            "WHILLY_USE_WORKSPACE": "0",
            "WHILLY_HEADLESS": "1",
        }
    )
    cmd = [sys.executable, "-m", "whilly", str(PLAN_PATH)]
    log(f"$ {' '.join(cmd)}")
    event("main.start", task_id=task.id, budget=BUDGET_USD)
    try:
        proc = subprocess.run(cmd, env=env, timeout=1800, check=False)
    except subprocess.TimeoutExpired:
        log("main agent timed out")
        event("main.done", task_id=task.id, ok=False, reason="timeout")
        return False

    event("main.done", task_id=task.id, ok=(proc.returncode == 0), exit_code=proc.returncode)
    return proc.returncode == 0


# ── Pipeline ───────────────────────────────────────────────────────────────────


def main() -> int:
    global _total_cost

    log(
        f"repo={REPO} label={LABEL} budget=${BUDGET_USD} max_loops={MAX_LOOPS} "
        f"backend={AGENT_BACKEND} dry_run={DRY_RUN} auto_merge={ALLOW_AUTO_MERGE}"
    )
    event("e2e.start", repo=REPO, label=LABEL, dry_run=DRY_RUN, auto_merge=ALLOW_AUTO_MERGE)

    if not os.environ.get("ANTHROPIC_API_KEY") and AGENT_BACKEND == "claude" and not DRY_RUN:
        log("ERROR: ANTHROPIC_API_KEY not set")
        return 0

    # ── 1) Fetch ──────────────────────────────────────────────────────────────
    section("1/8 fetch GitHub issues")
    try:
        plan_path, stats = fetch_github_issues(REPO, label=LABEL, out_path=PLAN_PATH, limit=10)
    except Exception as exc:  # noqa: BLE001
        log(f"fetch failed: {exc}")
        return 0
    log(f"plan={plan_path} new={stats.new} updated={stats.updated} total_open={stats.total_open}")

    tm = TaskManager(plan_path)
    ready = tm.get_ready_tasks()
    if not ready:
        log("no ready tasks — nothing to do")
        return 0
    task = ready[0]
    log(f"selected task: {task.id} (priority={task.priority})")

    # ── 2) Decision Gate ──────────────────────────────────────────────────────
    section("2/8 Decision Gate")
    if DRY_RUN:
        log("DRY_RUN: skip Decision Gate")
    else:
        d = decision_evaluate(task)
        _total_cost += d.cost_usd
        log(f"decision={d.decision} reason={d.reason} cost=${d.cost_usd:.4f}")
        event("decision_gate", task_id=task.id, decision=d.decision, reason=d.reason, cost_usd=d.cost_usd)
        if d.decision == REFUSE:
            label_flip_for_gh_task(task, d)
            tm.mark_status([task.id], "skipped")
            log("→ task refused, label flipped, exiting")
            return 0

    # ── 3) Branch ────────────────────────────────────────────────────────────
    section("3/8 prepare branch")
    branch = f"whilly/{task.id}"
    if not DRY_RUN:
        # Start from current main.
        _git("checkout", "main", cwd=Path.cwd())
        _git("pull", "--ff-only", "origin", "main", cwd=Path.cwd())
        # If the branch already exists locally, reuse.
        ck = _git("checkout", "-B", branch, "main", cwd=Path.cwd())
        if ck.returncode != 0:
            log(f"git checkout failed: {ck.stderr}")
            return 0
    log(f"branch: {branch}")

    # ── 4) Main agent ────────────────────────────────────────────────────────
    section("4/8 main agent run")
    main_ok = run_main_agent(task)
    if not main_ok:
        log("main agent failed — leaving repo as-is, exiting")
        return 0

    # Reload to see if status flipped to done.
    tm.reload()
    refreshed = tm.get_task(task.id)
    if not refreshed or refreshed.status != "done":
        log(f"main agent did not mark {task.id} done (status={refreshed.status if refreshed else 'missing'}) — exiting")
        return 0

    # ── 5) Open PR ────────────────────────────────────────────────────────────
    section("5/8 open PR")
    if DRY_RUN:
        log("DRY_RUN: skip PR creation")
        pr_number = "0"
    else:
        pr_result = open_pr_for_task(refreshed, worktree_path=Path.cwd())
        if not pr_result.ok:
            log(f"PR creation failed: {pr_result.reason}")
            return 0
        # Extract PR number from URL
        m = re.search(r"/pull/(\d+)", pr_result.pr_url)
        pr_number = m.group(1) if m else pr_result.pr_url
        log(f"PR opened: {pr_result.pr_url}")
        event("pr.opened", task_id=task.id, pr_url=pr_result.pr_url, pr_number=pr_number, branch=branch)

    # ── 6-7) Review-fix loop ─────────────────────────────────────────────────
    section(f"6/8 review-fix loop (max {MAX_LOOPS})")
    final_review: ReviewResult | None = None
    for iteration in range(1, MAX_LOOPS + 1):
        if _total_cost >= BUDGET_USD:
            log(f"budget exhausted (${_total_cost:.4f} ≥ ${BUDGET_USD}) — stopping loop")
            break

        review = review_pr(pr_number, refreshed)
        final_review = review
        log(
            f"iteration {iteration}: clean={review.clean} comments={len(review.comments)} "
            f"summary={review.summary[:80]!r}"
        )

        if review.clean:
            log("review CLEAN — exit loop")
            break

        # Post comments to PR.
        if not DRY_RUN:
            body = render_review_body(review, iteration)
            ok = gh_pr_review_comment(pr_number, body)
            log(f"posted review comment: ok={ok}")
            event(
                "review.posted",
                task_id=task.id,
                pr=pr_number,
                iteration=iteration,
                comment_count=len(review.comments),
            )

        # Run fixer.
        ok = run_fixer(refreshed, review, iteration, branch)
        if not ok:
            log("fixer failed — stopping loop")
            break

        # Brief pause to let GH index the new commits.
        time.sleep(2)
    else:
        log(f"reached max iterations ({MAX_LOOPS}) without clean review")

    # ── 7) Decide on merge ────────────────────────────────────────────────────
    section("7/8 merge decision")
    if final_review and final_review.clean:
        if DRY_RUN:
            log("DRY_RUN: would merge clean PR")
        elif ALLOW_AUTO_MERGE:
            ok = gh_pr_approve(pr_number)
            log(f"approved: {ok}")
            ok = gh_pr_merge(pr_number)
            log(f"merged: {ok}")
            event("pr.merged", task_id=task.id, pr=pr_number, auto=True)
        else:
            log("review CLEAN — but --allow-auto-merge not set; PR left for human merge")
            event("pr.left_open", task_id=task.id, pr=pr_number, reason="auto-merge disabled")
    else:
        log("review NOT clean after loop — leaving PR open with comments for human review")
        if not DRY_RUN and pr_number != "0":
            gh_pr_review_comment(
                pr_number,
                "🤖 Whilly e2e demo reached max review iterations without a clean review. "
                "Manual review required.",
            )
        event("pr.left_open", task_id=task.id, pr=pr_number, reason="max_iterations")

    # ── 8) Summary ────────────────────────────────────────────────────────────
    section("8/8 summary")
    log(f"total cost: ${_total_cost:.4f} (budget ${BUDGET_USD})")
    if final_review:
        log(f"final review: clean={final_review.clean}, comments={len(final_review.comments)}")
    event("e2e.done", task_id=task.id, total_cost_usd=_total_cost, clean=bool(final_review and final_review.clean))
    return 0


if __name__ == "__main__":
    sys.exit(main())
