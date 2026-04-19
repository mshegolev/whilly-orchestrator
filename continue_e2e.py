#!/usr/bin/env python3
"""Continue the e2e demo from step 5 — assumes branch whilly/GH-6 already
has the agent's commit. Pushes, opens PR, runs the review-fix loop, merges.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/tmp/whilly_e2e_run")

from whilly.agents import get_backend
from whilly.sinks.github_pr import open_pr_for_task
from whilly.task_manager import TaskManager

REPO = "mshegolev/whilly-orchestrator"
BRANCH = "whilly/GH-6"
TASK_ID = "GH-6"
PLAN = Path("/tmp/whilly_e2e_run/whilly_e2e_tasks.json")
MAX_LOOPS = 3
BUDGET_USD = 5.0
ALLOW_AUTO_MERGE = True
total_cost = 0.0


def log(m):
    print(f"[continue-e2e] {m}", flush=True)


def section(t):
    print(f"\n────── {t} ──────", flush=True)


def event(name, **kw):
    Path("/tmp/whilly_e2e_run/whilly_logs").mkdir(exist_ok=True)
    from datetime import datetime, timezone

    with open("/tmp/whilly_e2e_run/whilly_logs/whilly_events.jsonl", "a") as f:
        f.write(
            json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), "event": name, **kw},
                ensure_ascii=False,
            )
            + "\n"
        )


def _env_clean():
    e = dict(os.environ)
    e.pop("GITHUB_TOKEN", None)
    e.pop("GH_TOKEN", None)
    return e


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, env=_env_clean(), check=False, **kw)


def gh_pr_diff(pr):
    p = _run(["gh", "pr", "diff", str(pr), "--repo", REPO])
    return p.stdout if p.returncode == 0 else ""


def gh_pr_review(pr, kind, body):
    flag = "--request-changes" if kind == "changes" else "--approve"
    p = _run(["gh", "pr", "review", str(pr), "--repo", REPO, flag, "--body", body])
    if p.returncode != 0:
        log(f"gh pr review {kind} failed: {p.stderr.strip()}")
        return False
    return True


def gh_pr_merge(pr):
    p = _run(["gh", "pr", "merge", str(pr), "--repo", REPO, "--squash", "--delete-branch"])
    if p.returncode != 0:
        log(f"gh pr merge failed: {p.stderr.strip()}")
        return False
    return True


def _git(*args, cwd=None):
    return _run(["git", *args], cwd=str(cwd) if cwd else None)


REVIEWER_PROMPT = """Ты — строгий, но конструктивный code reviewer для Python-проекта whilly-orchestrator.

Тебе дан diff PR, который реализует следующую задачу:

----- TASK -----
ID: {task_id}
Description:
{description}

Acceptance criteria:
{acceptance}
----- END TASK -----

----- DIFF -----
{diff}
----- END DIFF -----

Сделай review. Найди:
- ошибки логики / баги
- нарушения acceptance criteria
- проблемы безопасности
- грубые style violations

Игнорируй nit-picks.

Ответ строго одной строкой валидного JSON, без пояснений и без блока ```:
{{"clean": true|false, "summary": "≤200 chars", "comments": [{{"file": "path", "severity": "blocker|major|minor", "message": "≤300 chars"}}]}}

Если diff полностью соответствует задаче — clean=true, comments=[].
"""

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
2. Применяй правки точечно.
3. Не добавляй новых features.
4. Когда закончишь — выведи строку <promise>COMPLETE</promise>.

После твоих изменений будет повторный review (всего до {max_loops} итераций).
"""


def parse_review(raw):
    if not raw:
        return False, "empty", []
    s = raw.strip()
    parsed = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r'\{[^{}]*"clean"\s*:\s*(?:true|false)[\s\S]*?\}', s)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    if not isinstance(parsed, dict):
        return False, "unparseable", []
    clean = bool(parsed.get("clean", False))
    summary = str(parsed.get("summary", ""))[:200]
    comments_raw = parsed.get("comments") or []
    comments = []
    for c in comments_raw if isinstance(comments_raw, list) else []:
        if isinstance(c, dict):
            comments.append(
                {
                    "file": str(c.get("file", "")),
                    "severity": str(c.get("severity", "minor")),
                    "message": str(c.get("message", ""))[:300],
                }
            )
    if clean and any(c["severity"] in ("blocker", "major") for c in comments):
        clean = False
        summary = (summary + " [inconsistent: clean=true with blocker comments]").strip()
    return clean, summary, comments


def render_review_body(summary, comments, iteration, cost):
    lines = [f"### Whilly review (iteration {iteration})", "", f"**Summary:** {summary or '(no summary)'}", ""]
    if comments:
        lines.append("**Comments:**")
        for c in comments:
            lines.append(f"- `[{c['severity']}]` `{c['file']}` — {c['message']}")
    else:
        lines.append("No comments.")
    lines.extend(["", "---", f"🤖 Generated by `continue_e2e.py` (cost ${cost:.4f})"])
    return "\n".join(lines)


def main():
    global total_cost
    log(f"resume from step 5 — branch={BRANCH} task={TASK_ID} budget=${BUDGET_USD}")
    event("e2e.resume", branch=BRANCH, task_id=TASK_ID)

    tm = TaskManager(PLAN)
    task = tm.get_task(TASK_ID)
    if task.status != "done":
        tm.mark_status([TASK_ID], "done")
        tm.reload()
        task = tm.get_task(TASK_ID)
        log(f"marked {TASK_ID} done in plan")

    cwd = Path("/tmp/whilly_e2e_run")

    # 5/8 push + open PR
    section("5/8 open PR")
    pr_result = open_pr_for_task(task, worktree_path=cwd)
    if not pr_result.ok:
        log(f"PR creation failed: {pr_result.reason}")
        return 1
    m = re.search(r"/pull/(\d+)", pr_result.pr_url)
    pr = m.group(1) if m else pr_result.pr_url
    log(f"PR opened: {pr_result.pr_url}")
    event("pr.opened", task_id=TASK_ID, pr_url=pr_result.pr_url, pr_number=pr, branch=BRANCH)

    # 6-7 review-fix loop
    section(f"6/8 review-fix loop (max {MAX_LOOPS})")
    backend = get_backend("claude")
    final_clean = False
    final_summary = ""
    final_comments = []

    for iteration in range(1, MAX_LOOPS + 1):
        if total_cost >= BUDGET_USD:
            log(f"budget exhausted (${total_cost:.4f}) — stopping")
            break

        diff = gh_pr_diff(pr)
        if len(diff) > 60000:
            diff = diff[:60000] + "\n... [truncated]"
        log(f"iteration {iteration}: diff {len(diff)} chars, calling reviewer")

        prompt = REVIEWER_PROMPT.format(
            task_id=task.id,
            description=task.description,
            acceptance="\n".join(f"- {a}" for a in task.acceptance_criteria) or "(none)",
            diff=diff,
        )
        event("review.start", iteration=iteration, diff_chars=len(diff))
        result = backend.run(prompt, timeout=180)
        total_cost += result.usage.cost_usd
        event(
            "review.done",
            iteration=iteration,
            cost_usd=result.usage.cost_usd,
            running_total=total_cost,
        )

        clean, summary, comments = parse_review(result.result_text)
        log(f"  → clean={clean} comments={len(comments)} cost=${result.usage.cost_usd:.4f} summary={summary[:80]!r}")
        final_clean, final_summary, final_comments = clean, summary, comments

        if clean:
            log("review CLEAN — exit loop")
            break

        body = render_review_body(summary, comments, iteration, result.usage.cost_usd)
        gh_pr_review(pr, "changes", body)

        # Fixer
        log(f"running fixer (iteration {iteration})")
        comments_block = "\n".join(f"- [{c['severity']}] {c['file']}: {c['message']}" for c in comments) or "(no specific)"
        fix_prompt = FIXER_PROMPT.format(
            task_id=task.id,
            description=task.description,
            acceptance="\n".join(f"- {a}" for a in task.acceptance_criteria) or "(none)",
            comments=comments_block,
            max_loops=MAX_LOOPS,
        )
        event("fix.start", iteration=iteration, comment_count=len(comments))
        fix_result = backend.run(fix_prompt, timeout=900, cwd=cwd)
        total_cost += fix_result.usage.cost_usd
        event(
            "fix.done",
            iteration=iteration,
            cost_usd=fix_result.usage.cost_usd,
            is_complete=fix_result.is_complete,
            running_total=total_cost,
        )
        log(f"  → fixer cost=${fix_result.usage.cost_usd:.4f} complete={fix_result.is_complete}")

        # Commit + push
        _git("add", "-A", cwd=cwd)
        if _git("diff", "--cached", "--quiet", cwd=cwd).returncode == 0:
            log("  fixer made no changes — skip commit")
            continue
        _git("commit", "-m", f"fix({task.id}): address review iteration {iteration}", cwd=cwd)
        push_proc = _git("push", "origin", f"HEAD:{BRANCH}", "--force-with-lease", cwd=cwd)
        if push_proc.returncode != 0:
            log(f"  push failed: {push_proc.stderr.strip()}")
        time.sleep(2)
    else:
        log(f"reached max iterations ({MAX_LOOPS}) without clean review")

    # 7/8 merge decision
    section("7/8 merge decision")
    if final_clean:
        if ALLOW_AUTO_MERGE:
            ok = gh_pr_review(pr, "approve", "Whilly: clean review. Approving.")
            log(f"approved: {ok}")
            ok = gh_pr_merge(pr)
            log(f"merged: {ok}")
            event("pr.merged", task_id=task.id, pr=pr, auto=True)
        else:
            log("clean — but auto-merge disabled, leaving PR open")
    else:
        log("not clean after loop — leaving PR open with comments")
        gh_pr_review(
            pr,
            "changes",
            "🤖 Whilly e2e demo reached max review iterations without clean review. Manual review required.",
        )

    # 8/8 summary
    section("8/8 summary")
    log(f"total cost (review+fix only): ${total_cost:.4f}")
    log(f"final review: clean={final_clean} comments={len(final_comments)}")
    event("e2e.done", task_id=task.id, total_cost_usd=total_cost, clean=final_clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())
