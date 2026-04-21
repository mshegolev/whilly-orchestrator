---
title: Task Execution Phases
layout: default
parent: Documents
nav_order: 1
description: "Eight phases Whilly walks through when it picks up a GitHub issue and merges the result."
---

# Task Execution Phases
{: .fs-9 }

What happens inside Whilly from the moment it fetches a GitHub issue to the moment it exits `0`.
{: .fs-5 .fw-300 }

---

**TL;DR** — Whilly orchestrates. The Claude CLI agent writes the code. Whilly handles workspace isolation, retry logic, budget enforcement, and board sync; the agent handles everything inside the editor.
{: .fs-3 }

---

## Overview

Running `whilly --from-issue REPO#N --go --headless` triggers a linear sequence of 8 internal phases. The sequence is deterministic: each phase either succeeds and advances, or fails with a logged reason and a non-zero exit code. No phase is optional; no phase is re-ordered.

The diagram below shows the happy path. The branch at phase 6 is not a fork — both the plan file update and the Projects v2 board sync happen in that single phase before control continues to phase 7.

```
 ┌─────────────────────────────────────────────────────────┐
 │  whilly --from-issue REPO#N --go --headless             │
 └──────────────────────┬──────────────────────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 1 — Fetch issue                   │
 │  gh api /repos/.../issues/N              │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 2 — Generate plan                 │
 │  tasks-issue-{repo}-{N}.json             │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 3 — Workspace                     │
 │  git worktree add .whilly_workspaces/    │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 4 — Agent runs          [code]    │
 │  claude CLI in tmux session              │
 │  status: Todo → In Progress              │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 5 — Parse result                  │
 │  AgentResult: exit_code, cost, complete  │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 6 — Mark done + live sync         │
 │  status: In Progress → In Review         │
 │  ├── plan JSON updated                   │
 │  └── Projects v2 card moved              │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 7 — Commit                        │
 │  agent: git add + git commit             │
 └──────────────────────┬───────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────┐
 │  Phase 8 — Exit 0                        │
 │  run_plan returns → caller continues     │
 └──────────────────────────────────────────┘
```

---

## Phase table

| # | Phase | Code location | What happens |
|---|---|---|---|
| 1 | Fetch issue | `whilly/sources/fetch_single_issue` | `gh api /repos/.../issues/N` pulls title, body, and labels |
| 2 | Generate plan | `whilly/cli.py` ~L2300 | Writes `tasks-issue-{repo}-{N}.json`; extracts acceptance criteria from body checklists |
| 3 | Workspace | `worktree_runner.create_plan_workspace` | `git worktree add .whilly_workspaces/{slug}/ -b whilly/workspace/{slug}` |
| 4 | Agent runs | `cli.py::run_plan` + `agent_runner` + `tmux_runner` | Spawns `claude` CLI in tmux session `whilly-{task_id}`; waits for completion signal |
| 5 | Parse result | `agent_runner.collect_result` | Reads Claude CLI JSON → `AgentResult(exit_code, usage.cost_usd, is_complete)` |
| 6 | Mark done + live sync | `task_manager.TaskManager.mark_status` + `project_board` | `status: done` in JSON; Projects v2 card moves to "In Review" |
| 7 | Commit | agent itself | Agent runs `git add` + `git commit` inside the worktree; commits land on `whilly/workspace/{slug}` |
| 8 | Exit | `cli.py::run_plan` returns `0` | Control returns to the caller (`whilly-auto.sh`, another script, or the user's shell) |

**Whilly does not write code.** Phase 4 delegates entirely to the Claude CLI agent. Whilly's only job is to construct the prompt, launch the process, and interpret the result.
{: .note }

---

## Phase 2 — Generate plan

The plan file is a single-task JSON document written to the current working directory (or the workspace root if `USE_WORKSPACE` is on):

```
tasks-issue-{owner}-{repo}-{N}.json
```

Minimum fields the orchestrator cares about:

| Field | Value |
|---|---|
| `id` | `issue-{N}` |
| `status` | `pending` |
| `description` | Issue title + full body |
| `acceptance_criteria` | Extracted from `- [ ]` checklists in the issue body |
| `priority` | Inferred from labels; defaults to `medium` |
| `dependencies` | `[]` for single-issue plans |
| `key_files` | `[]` initially; agent may populate on retry |

The schema is validated by `cli.validate_schema` before the plan is accepted. Note that `validate_schema` checks only the first 3 tasks — plans larger than 3 tasks pass structural validation with only partial coverage.

---

## Phase 3 — Workspace isolation

Whilly creates (or reuses) a **git worktree** — not a clone — so the agent works on an isolated copy of the repository. The worktree branch name is deterministic:

```
whilly/workspace/{slug}
```

where `{slug}` is derived from the plan filename. The main repo's working tree is never touched by the agent during execution.

### Worktree, not clone

`git worktree add` is fundamentally different from `git clone`. Both give you an isolated working directory, but the cost and the semantics are not the same.

| | `git clone` | `git worktree add` (what Whilly uses) |
|---|---|---|
| Network traffic | yes — pulls objects from the remote | none — fully local |
| `.git/` directory | separate copy | shared with the main repo (one `.git/` directory serves both) |
| Object storage | duplicated on disk | not duplicated — one set of blobs/commits is referenced from both |
| Branches | remote refs are a second universe | all branches from the main repo are visible immediately |
| Creation speed | seconds to minutes | milliseconds |
| Isolation | complete | working files are isolated; history and objects are shared |

Concretely, when Phase 3 runs, the command executed is:

```bash
git worktree add .whilly_workspaces/{slug}/ -b whilly/workspace/{slug} HEAD
```

That creates:

- A new directory `.whilly_workspaces/{slug}/` with the files checked out.
- A new branch `whilly/workspace/{slug}` rooted at the current `HEAD`.
- A plain file called `.git` inside the worktree (not a directory) that points back to the shared `.git/` in the main repo.

The agent reads, edits, and commits inside `.whilly_workspaces/{slug}/`. Commits land in the **same** `.git/objects/` store as the main repo — there is no duplication of history — but they live on a separate branch. This is why a 5 GB repo creates a worktree in milliseconds without using another 5 GB of disk.

Uncommitted changes in the main working tree are not visible in the worktree, and vice versa. The two directories have fully independent file states. Only the branch graph and object store are shared.
{: .note }

### Where cloning actually happens

A `git clone` only happens once — the first time you check out the repository on your machine. After that, `scripts/whilly-auto.sh` keeps the local checkout current with an incremental `git fetch origin $BASE_BRANCH` (documented in phase 0.1 of the script), which downloads only new commits, not the whole repo. The worktree itself is a purely local operation.

The `run_plan` function `chdir`s into the worktree before dispatching any agents. The plan file itself is resolved to an absolute path *before* the chdir so the orchestrator always reads and writes the canonical JSON in the main repo, not the worktree copy.

If you kill Whilly mid-run, the worktree remains on disk. On the next `--resume`, Whilly reuses it. To clean up manually:

```bash
git worktree remove .whilly_workspaces/{slug}/ --force
```

### Base branch freshness

The worktree branches from the current `HEAD` of the checkout that runs Whilly. If your local `main` is behind `origin/main`, the agent works on outdated code and the eventual PR diff will be wrong. Whilly itself does not fetch — the caller is responsible for keeping the base branch current.

Always `git fetch origin && git pull --ff-only` the base branch before running Whilly, or let `scripts/whilly-auto.sh` do it automatically (it runs the equivalent of `git fetch + git checkout main + git pull --ff-only` before phase 1, and supports `SKIP_SYNC=1` for offline runs).
{: .warning }

---

## Phase 4 — Agent runs

This is the only phase where code is written, and Whilly does not do the writing.

### What Whilly does

1. Marks the task `in_progress` in the plan JSON and on the Projects v2 board (`Todo → In Progress`).
2. Builds a prompt via `cli.build_task_prompt` that references `@tasks.json` and `@progress.txt`, pins the agent to a single `task_id`, and requires `make lint` / `make test` to pass before signalling completion.
3. Spawns the Claude CLI in a named tmux session `whilly-{task_id}`:
   ```
   tmux new-session -d -s whilly-{task_id} 'claude --task-id ... --output-format json ...'
   ```
4. Polls the session output file and the plan JSON until it sees either the completion signal or an error exit code.

### Completion signal

The agent must emit the following literal string anywhere in its reply output for Whilly to treat the task as done:

```
<promise>COMPLETE</promise>
```

If this string is absent, Whilly treats the run as incomplete and retries.

If `<promise>COMPLETE</promise>` is missing from the agent output, Whilly retries up to `MAX_TASK_RETRIES` (default: 5) times with exponential backoff (5 / 10 / 20 / 40 / 60 seconds). After 5 failures, the task is marked `failed`.
{: .note }

### Retry and deadlock detection

| Condition | What Whilly does |
|---|---|
| API error (5xx, network) | Retry with backoff; up to `MAX_TASK_RETRIES` |
| Auth error (401/403) | Mark `failed` immediately; no retry |
| Missing completion signal | Retry with backoff |
| Task `in_progress` for ≥ 3 iterations without progress | Force-mark `skipped` |

A task stuck `in_progress` for 3 or more consecutive iterations without any other task completing is treated as a deadlock and force-marked `skipped`. This prevents Whilly from hanging indefinitely on a task the agent cannot finish.
{: .warning }

`WHILLY_BUDGET_USD` — if total spend reaches 100% of the configured budget, Whilly kills all active tmux sessions and exits immediately with code `2`. An 80% spend triggers a logged warning but does not stop execution.
{: .warning }

---

## Phase 6 — Mark done and live sync

Phase 6 is two writes in sequence:

1. `TaskManager.mark_status(task, "done")` — atomically writes `status: done` to the plan JSON on disk. The file is the source of truth; the orchestrator re-reads it after every write.
2. `project_board.set_task_status(task, "in_review")` — moves the corresponding Projects v2 card from "In Progress" to "In Review" via GraphQL.

### Status transition summary

| Phase | Transition | Trigger |
|---|---|---|
| Phase 4 start | `Todo → In Progress` | Agent launched |
| Phase 6 | `In Progress → In Review` | Agent reported complete |
| After PR merge | `In Review → Done` | `whilly --post-merge <plan>` |

`In Review → Done` does **not** happen inside this 8-phase flow. It is triggered separately by `whilly --post-merge <plan>` after the pull request actually merges. See [GitHub Integration Guide]({{ site.baseurl }}/GitHub-Integration-Guide) for the full lifecycle.

---

## Try it

```bash
whilly --from-issue mshegolev/whilly-orchestrator#160 --go
```

Without `--headless`, Whilly displays the Rich TUI dashboard during execution. The dashboard shows active tmux sessions, per-task token and cost counters, and iteration progress. Hotkeys:

| Key | Action |
|---|---|
| `q` | Quit (graceful) |
| `d` | Detail view for the focused task |
| `l` | Live log tail |
| `t` | Task list |
| `h` | Help overlay |

Add `--headless` to suppress the TUI and emit structured JSON on stdout instead. Exit codes: `0` = all done, `1` = some tasks failed, `2` = budget exceeded, `3` = timeout.

---

## Relation to `whilly-auto.sh`

The 8 phases described on this page correspond to **step 2** of the outer pipeline defined in `scripts/whilly-auto.sh`. The full pipeline is:

```
Step 1 — validate inputs + branch guard
Step 2 — whilly --from-issue ... --go --headless   ← phases 1–8 documented here
Step 3 — git push origin whilly/workspace/{slug}
Step 4 — gh pr create ...
Step 5 — gh pr merge --auto ...
Step 6 — whilly --post-merge <plan>                ← In Review → Done
```

Phases 1–8 are self-contained: Whilly exits `0` after phase 8 and returns control to the script, which then handles the push, PR creation, merge, and post-merge status update. The worktree is left intact so the push in step 3 can read the commits made in phase 7.

See [`scripts/whilly-auto.sh`](https://github.com/mshegolev/whilly-orchestrator/blob/main/scripts/whilly-auto.sh) in the repository for the complete outer loop.

---

## See also

- [Full Usage Reference]({{ site.baseurl }}/Whilly-Usage) — every CLI flag, env var, and config field, including all `WHILLY_*` variables referenced on this page
- [Interfaces and Tasks]({{ site.baseurl }}/Whilly-Interfaces-and-Tasks) — module contracts and the full JSON plan schema
- [GitHub Integration Guide]({{ site.baseurl }}/GitHub-Integration-Guide) — Projects v2 setup, board sync, and the post-merge lifecycle
