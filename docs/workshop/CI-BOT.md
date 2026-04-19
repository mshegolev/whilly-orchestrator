---
title: Whilly CI Bot — setup & operation
type: ops-guide
created: 2026-04-20
status: v1
related: [INDEX.md, BRD-Whilly.md, PRD-Whilly.md, adr/ADR-012-self-hosting-bootstrap-demo.md]
---

# Whilly CI Bot

> **EN:** GitHub Actions workflow that runs whilly on a schedule, picks open issues with label `whilly:ready`, and opens PRs to fix them. The full self-hosting bootstrap demo, but autonomous.
>
> **RU:** Workflow в GitHub Actions, запускающий whilly по расписанию. Подбирает open-issues с label `whilly:ready` и открывает PR'ы. Self-hosting bootstrap demo в автономном режиме.

---

## Architecture

```
                        ┌─────────────────────┐
                        │  GitHub Actions     │
                        │  whilly-bot.yml     │
                        └──────────┬──────────┘
                                   │
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
       schedule:             issues.labeled:         workflow_dispatch
       cron 03:00 UTC        whilly:ready            (manual)
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │ scripts/whilly_ci.py│
                        └──────────┬──────────┘
                                   │
       ┌───────────────┬───────────┼───────────┬───────────────┐
       ▼               ▼           ▼           ▼               ▼
   fetch_github    Decision    whilly       open_pr        upload
   _issues         Gate        headless     _for_task      artifact
   (sources/)      (refuse →   (loop with   (sinks/)       (logs)
                   label flip) budget cap)
```

---

## Setup (one-time)

### 1. Add `ANTHROPIC_API_KEY` repo secret

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic API key (starts with `sk-ant-…`) |

`GITHUB_TOKEN` is provided automatically by Actions — no setup needed.

### 2. Confirm labels exist

The workflow expects two labels on the repo:

```bash
gh label create "whilly:ready" --color "0E8A16" \
  --description "Ready for whilly orchestrator pickup"
gh label create "needs-clarification" --color "FBCA04" \
  --description "Decision Gate refused: needs more detail"
```

Already present in `mshegolev/whilly-orchestrator`.

### 3. Verify the workflow file is on `main`

```bash
gh workflow list --repo mshegolev/whilly-orchestrator
# should include: Whilly Bot   active   .github/workflows/whilly-bot.yml
```

### 4. (Optional) Tune the cron schedule

Edit `.github/workflows/whilly-bot.yml`:

```yaml
on:
  schedule:
    - cron: "0 3 * * *"      # daily at 03:00 UTC
    # Other examples:
    # - cron: "0 */6 * * *"   # every 6 hours
    # - cron: "0 9 * * 1-5"   # weekdays at 09:00 UTC
```

---

## Triggers

| Trigger | When fires | Default behaviour |
|---|---|---|
| `schedule` (cron) | Daily at 03:00 UTC | 1 task, $0.50 budget |
| `issues.labeled` | When `whilly:ready` is added to an issue | 1 task, $0.50 budget |
| `workflow_dispatch` | Manual via Actions UI or `gh workflow run` | configurable inputs |

### Manual trigger via CLI

```bash
gh workflow run whilly-bot.yml --repo mshegolev/whilly-orchestrator \
  -f max_tasks=2 \
  -f budget_usd=1.0 \
  -f dry_run=false
```

### Dry-run mode (test without spending tokens)

```bash
gh workflow run whilly-bot.yml -f dry_run=true
```

Or set the input via UI checkbox. Dry-run skips the Decision Gate LLM call, the agent run, and PR creation — only the `fetch_github_issues` step runs.

---

## Cost control

| Guardrail | Default | What it does |
|---|---|---|
| `WHILLY_BUDGET_USD` | `0.5` per run | Hard-stops the loop at 100% of cap (warning at 80%). |
| `WHILLY_MAX_TASKS` | `1` per run | Limits how many issues a single run touches. |
| Decision Gate auto-refuse | `<20` chars | Auto-skips trash issues without an LLM call. |
| `concurrency: whilly-bot` | locked | Prevents two parallel runs (no double-spend). |
| `timeout-minutes: 45` | belt+suspenders | Job killed after 45 min even if whilly hangs. |

**Monthly worst-case cost (cron daily, $0.50 cap, every run hits cap):**
- 30 runs × $0.50 = **$15/month** even if every run maxes out.
- Realistically: most runs find no ready issues, cost $0.

**Pause the bot:** Settings → Actions → Workflows → Whilly Bot → **Disable workflow**.

---

## Observability

Each run uploads `whilly_logs/whilly_events.jsonl` and the generated `whilly_ci_tasks.json` as a workflow artifact (retained 14 days).

### Quick checks via gh

```bash
# Recent runs
gh run list --workflow=whilly-bot.yml --limit 10

# View a specific run
gh run view <run-id>

# Download artifact
gh run download <run-id> --name whilly-logs-<run-id>

# Then locally:
jq 'select(.event=="task.done") | {task_id, cost_usd, duration_s}' \
   whilly_logs/whilly_events.jsonl
```

### Spending overview (last N runs)

```bash
for run in $(gh run list --workflow=whilly-bot.yml --limit 10 --json databaseId -q '.[].databaseId'); do
  gh run download "$run" --name "whilly-logs-$run" -D /tmp/whilly-runs/"$run" 2>/dev/null
  total=$(jq -s 'map(select(.event=="task.done") | .cost_usd) | add // 0' \
              /tmp/whilly-runs/"$run"/whilly_logs/whilly_events.jsonl 2>/dev/null)
  echo "run $run: total cost = \$$total"
done
```

---

## Security

| Concern | Mitigation |
|---|---|
| Secrets leak via prompt | `--dangerously-skip-permissions` is fine **inside** Actions VM (ephemeral), but **never** point bot at issues containing real secrets. |
| Forked PR triggering | `issues.labeled` requires write permissions on labels — forks **cannot** trigger. |
| Direct push to `main` | Bot **never** pushes to `main` — only to `whilly/GH-N` branches via `--force-with-lease`. |
| Auto-merge | **Disabled by design** — every PR requires human review. |
| Token scope | Built-in `GITHUB_TOKEN` is repo-scoped — bot cannot touch other repos. |
| Runaway agent | `WHILLY_TIMEOUT` + job `timeout-minutes: 45` + `WHILLY_BUDGET_USD` triple guard. |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY not set` in logs | Repo secret missing | Settings → Secrets → add it |
| `claude: command not found` | Wrong npm package name | Check `npm install -g @anthropic-ai/claude-code` step; the package name has changed historically |
| `gh issue list failed: HTTP 401` | `GITHUB_TOKEN` lacks scope | Verify `permissions:` block in workflow — needs `issues: write`, `pull-requests: write` |
| Bot opens duplicate PRs | Same issue picked across runs | Check labels — bot should remove `whilly:ready` after picking (TODO in next iteration) |
| All tasks refused by Decision Gate | Issue descriptions too short | Ensure issues have ≥20 chars description + acceptance criteria |
| Job killed at 45 min | whilly stuck in retry loop | Lower `WHILLY_MAX_TASK_RETRIES`, raise `WHILLY_TIMEOUT` < 45min |
| Push fails: "permission denied" | `contents: write` missing | Add to `permissions:` block in workflow |
| Bot runs but no PRs opened | Tasks didn't reach `done` | Check `whilly_logs/whilly_events.jsonl` artifact — look for `task.failed` / `task.skipped` |

---

## What this enables

- **HackSprint1 minimum requirement #4 (retry-loop):** the workflow inherits whilly's built-in retry on transient errors.
- **HackSprint1 minimum requirement #5 (real repo):** runs on `mshegolev/whilly-orchestrator` itself.
- **Optional block "Observability":** JSONL events + artifact upload + cost summary.
- **Optional block "Quality gates":** can be added by chaining a CI workflow that runs on the bot's PR.
- **Demo for voting:** "Watch whilly close its own GitHub issue, autonomously, every night" — strong narrative.

---

## Roadmap (post-demo)

- Auto-remove `whilly:ready` after pickup (avoid duplicate PRs).
- Cost dashboard via GitHub Pages (parsed from JSONL artifacts).
- Slack notification on PR open (optional `--slack-webhook` flag).
- Multi-repo support (single bot serves several repos via matrix strategy).
- Budget enforced via `WHILLY_BUDGET_USD` per *month* (not just per run) via state file.

---

**Status:** v1 · 2026-04-20 · paired with `.github/workflows/whilly-bot.yml` and `scripts/whilly_ci.py`.
