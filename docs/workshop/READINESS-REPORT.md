# Whilly Workshop Readiness Report
**Status:** v1 · 2026-04-20 · scope source: `HackSprint1_PRD_AgentsOrchestrator.md`

> **EN:** Honest gap analysis between **whilly-orchestrator** (current `main`, v3.0.0, 6244 LOC) and the **HackSprint1** workshop scope. What works out of the box, what we still need to ship before we can run the workshop end-to-end with the *self-hosting bootstrap* demo.
>
> **RU:** Честный gap-анализ между **whilly-orchestrator** (`main`, v3.0.0, 6244 LOC) и scope воркшопа **HackSprint1**. Что работает из коробки, что нужно дописать до воркшопа, чтобы прогнать demo «self-hosting bootstrap» end-to-end.

---

## TL;DR

| Layer | Coverage | Verdict |
|---|---|---|
| Supervisor loop | ✅ 100% | Production-grade. Ralph loop, batching, deadlock guard, budget. |
| Worker isolation | ✅ 100% | tmux + git worktree, per-task workspace. Beats both grkr (1 backend) and yolo (Go-only). |
| Agent backend | ✅ Claude CLI | Single backend. Codex/Gemini = stretch. |
| State + recovery | ✅ 100% | `.whilly_state.json` resume, atomic writes via TaskManager. |
| Logging / events | ✅ JSONL | `whilly_logs/whilly_events.jsonl`. Format documented. |
| Dashboard / TUI | ✅ Rich Live | Hotkeys (q/p/d/l/t/h). NullDashboard for headless. |
| Source = `tasks.json` | ✅ 100% | Atomic JSON file, schema validated. |
| **Source = GitHub Issues** | ❌ **0%** | **GAP — blocks self-hosting demo.** |
| **Sink = `gh pr create`** | ❌ **0%** | **GAP — blocks self-hosting demo.** |
| **Decision Gate** | ❌ **0%** | GAP — blocks "agent refuses unclear issues" UX. |
| PRD wizard | ✅ | Goes beyond HackSprint1 scope. |
| TRIZ analyzer | ✅ | Goes beyond HackSprint1 scope. |
| Decomposer | ✅ | Goes beyond HackSprint1 scope. |
| Workshop tutorial | ⚠️ Partial | README + Usage exist, hands-on tutorial = GAP. |
| Sample tasks / sample issues | ❌ | GAP — needs `examples/workshop/tasks.json` + 5-10 demo GH issues. |

**Verdict:** Whilly is **technically ahead of the HackSprint1 MVP target**. The remaining workshop blockers are the **GitHub source/sink pair**, the **Decision Gate**, and the **hands-on tutorial pack**. These are scoped in [ROADMAP.md](ROADMAP.md) and tracked as the **gap pack** for this sprint.

---

## 1. Coverage matrix vs HackSprint1 PRD

Section IDs reference the original `HackSprint1_PRD_AgentsOrchestrator.md`.

### 1.1 Must-have (PRD §4)

| PRD requirement | Whilly module | Status | Notes |
|---|---|---|---|
| Source adapter (1 source) | `task_manager.py` | ✅ JSON | Reads `tasks.json`. **GitHub Issues source missing.** |
| Supervisor loop | `cli.py::run_plan` | ✅ | Continuous loop, batch planning, deadlock + budget guards. |
| Worker (git worktree) | `worktree_runner.py` | ✅ | Per-plan + per-task isolation, cherry-pick back. |
| Agent backend (1 LLM) | `agent_runner.py` | ✅ | Claude CLI subprocess, JSON output parsing, usage accounting. |
| State file | `state_store.py` | ✅ | `.whilly_state.json`, `--resume` support. |
| JSONL events | `cli.py::_log_event` | ✅ | `whilly_logs/whilly_events.jsonl`. |

### 1.2 Nice-to-have (PRD §4)

| PRD requirement | Whilly module | Status | Notes |
|---|---|---|---|
| Decision Gate (proceed/refuse) | — | ❌ | **GAP.** Agent always tries; no upfront refuse. |
| PR creation | — | ❌ | **GAP.** Whilly commits, never opens PR. |
| TUI dashboard | `dashboard.py` | ✅✅ | Goes beyond — Rich Live, hotkeys, multi-pane. |
| Pluggable backends | `agent_runner.py` | ⚠️ | Interface exists implicitly; only Claude is implemented. Adding Codex = stretch (~half day). |

### 1.3 Bonus (not in HackSprint1 PRD, exists in whilly)

| Feature | Whilly module | Value for workshop |
|---|---|---|
| PRD wizard / generator | `prd_wizard.py`, `prd_generator.py` | Demo path: PRD → tasks.json in 1 command. |
| TRIZ analyzer | `triz_analyzer.py` | Edge case for advanced track. |
| LLM-based batching | `orchestrator.py::plan_batches_llm` | Stretch demo. |
| Task decomposer | `decomposer.py` | Mid-run subtask creation. |
| Notifications | `notifications.py` | Budget warning, deadlock, auth alert. |
| Reports + Markdown summary | `reporter.py` | Per-iteration JSON + end-of-run MD. |
| History DB | `history.py` | Cross-run analytics. |
| Web status | `web_status.py` | HTTP read-only status endpoint. |

---

## 2. Workshop demo readiness checklist

Demo target (per user choice in kickoff): **whilly self-hosting** — whilly closes its own GitHub issues, opens its own PRs, the human reviews and merges.

| # | Step in demo | Required gap-pack item | Status |
|---|---|---|---|
| 1 | Demo machine has Claude CLI + Anthropic key | external prereq | ✅ |
| 2 | Repo `mshegolev/whilly-orchestrator` is public, has open issues | seed 5-10 issues with `whilly:ready` label | ❌ — to do |
| 3 | `whilly --source gh:mshegolev/whilly-orchestrator` reads issues | `whilly/sources/github_issues.py` | ❌ — to do |
| 4 | Loop picks 1 ready issue, hands to agent | reuses existing TaskManager flow | ✅ pending #3 |
| 5 | Agent works in isolated worktree | reuses existing worktree_runner | ✅ |
| 6 | After done → `gh pr create`, PR linked to issue | `whilly/sinks/github_pr.py` | ❌ — to do |
| 7 | Decision Gate refuses 1 unclear issue (label flip) | `whilly/decision_gate.py` | ❌ — to do |
| 8 | Live dashboard shows progress, cost, JSONL stream | reuses existing dashboard | ✅ |
| 9 | Tutorial walks audience through steps 1-7 | `docs/workshop/TUTORIAL.md` | ❌ — to do |
| 10 | Sample `tasks.json` for participants without GitHub auth | `examples/workshop/tasks.json` | ❌ — to do |

**6 items missing**, all small/medium scope. See [ROADMAP.md](ROADMAP.md) for breakdown and [PRD-Whilly.md](PRD-Whilly.md) for acceptance criteria.

---

## 3. Strengths over reference projects

| Aspect | grkr (bash) | yolo-runner (Go) | **whilly (Python)** |
|---|---|---|---|
| Onboarding for Python team | low (bash) | medium (Go) | **highest** |
| Parallelism | none | concurrency flag | **tmux + worktree, batch planning** |
| Dashboard | none | Bubble Tea TUI | **Rich Live, hotkeys** |
| State recovery | basic | yes | **`.whilly_state.json`, --resume** |
| Decomposer / TRIZ | no | no | **yes** |
| PRD wizard | no | no | **yes** |
| Decision Gate | yes | no | **planned (gap pack)** |
| GitHub Issues source | yes | yes (multi) | **planned (gap pack)** |
| PR creation | yes | yes | **planned (gap pack)** |
| Pluggable backends | 1 (Codex) | 5+ | 1 (Claude) — stretch |
| LOC | ~200 (bash) | ~30k (Go) | 6244 (Python) |

Whilly's **niche**: a Python-native, batteries-included orchestrator with a real TUI, sitting between the "everything is bash" minimalism of grkr and the "build your own Go framework" weight of yolo-runner.

---

## 4. Risks for the workshop

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| W1 | Participants don't have `gh` auth → PR demo fails | High | Medium | Fall back to `tasks.json` track. Pre-flight check in TUTORIAL.md. |
| W2 | Anthropic rate limits during live demo | Medium | High | Pre-record demo, have $0.50 safety budget. Show JSONL replay. |
| W3 | `gh` CLI version mismatch (need ≥2.40 for some flags) | Medium | Low | Pin minimum in TUTORIAL prerequisites. |
| W4 | Whilly TUI breaks on small terminal (<100 cols) | Medium | Low | `--headless` mode documented as fallback. |
| W5 | Self-hosting demo fails: agent can't write inside worktree | Low | High | Smoke-tested before workshop, fallback to local plan. |
| W6 | Budget cap kicks in mid-demo | Low | Medium | Set `WHILLY_BUDGET_USD=10` for demo, warn at 80%. |
| W7 | Workshop materials drift from code | High over time | Medium | Sync script `scripts/sync_workshop_docs.sh` + CI check. |

---

## 5. Cut-line for "minimum viable workshop"

If we have only **2 hours** before workshop start:

- ✅ TUTORIAL.md "Hour 1: install + first run on tasks.json" — works today, no code change.
- ❌ Hours 2-6 require gap pack code → schedule full afternoon block.

If we have **1 day** (this session's target):

- ✅ Ship gap pack: GH source + PR sink + Decision Gate + tutorial.
- ✅ Bilingual INDEX, BRD, PRD, ADR.
- ⚠️ Recorded demo video = stretch.

---

## 6. Open questions deferred to the workshop session

These are **not blockers** but should be discussed live with participants:

- Should we add MCP server interface for whilly (publish whilly tools as MCP)?
- Multi-agent backend dispatch (round-robin Claude/Codex) — worth a third workshop hour?
- Linear / Jira adapters — when?
- Web UI vs Rich TUI — keep both?

---

**Bottom line:** whilly already implements **more than the HackSprint1 MVP target**. The remaining 6 workshop blockers are scoped, small, and tracked. After the gap pack lands, whilly is ready to run the self-hosting bootstrap demo end-to-end.
