---
title: Whilly Workshop — Roadmap & Decomposition
type: roadmap
created: 2026-04-20
status: v1
related: [PRD-Whilly.md, READINESS-REPORT.md]
---

# Whilly Workshop Roadmap

> **EN:** Decomposition of the gap pack into atomic tasks. Each row maps to (a) a planned commit, (b) a `tasks.json` entry, and (c) a GitHub issue with label `whilly:ready`. This document is the **single source of truth** for what ships in the current cycle.
>
> **RU:** Декомпозиция gap-пака в атомарные задачи. Каждая строка соответствует (a) запланированному коммиту, (b) записи в `tasks.json`, (c) issue в GitHub с label `whilly:ready`.

---

## Phase plan

```
Phase 1 — Documents & scaffolding              [DONE in this session]
Phase 2 — Code: GH source adapter              [in progress]
Phase 3 — Code: PR sink                        [pending, depends on Phase 2]
Phase 4 — Code: Decision Gate                  [pending, can parallel Phase 3]
Phase 5 — Workshop UX (tutorial, samples)      [pending, depends on 2-4]
Phase 6 — README + bilingual + Obsidian sync   [pending, depends on 5]
Phase 7 — Tests & lint hardening               [parallel with 2-6]
Phase 8 — Demo dry-run + recording             [pending, depends on all]
```

---

## Decomposed task list

> Each task here is intended to map 1:1 to a GitHub Issue (label `whilly:ready`) and a `tasks.json` entry in `examples/workshop/tasks.json`. Status reflects what's done **in this session**.

### Phase 1 — Documents & scaffolding (✅ done)

| ID | Task | Files | Status |
|---|---|---|---|
| WS-001 | Create `docs/workshop/` structure + INDEX.md | `docs/workshop/INDEX.md` | ✅ |
| WS-002 | Write BRD-Whilly.md (RU primary, EN summary) | `docs/workshop/BRD-Whilly.md` | ✅ |
| WS-003 | Write PRD-Whilly.md | `docs/workshop/PRD-Whilly.md` | ✅ |
| WS-004 | Write READINESS-REPORT.md | `docs/workshop/READINESS-REPORT.md` | ✅ |
| WS-005 | Write ROADMAP.md (this file) | `docs/workshop/ROADMAP.md` | 🟡 in progress |
| WS-006 | Write ADR pack (12 records) | `docs/workshop/adr/*.md` | ⏳ next |

### Phase 2 — GitHub Issues source adapter

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| GH-101 | Create `whilly/sources/__init__.py` package | `whilly/sources/__init__.py` | imports clean | 5m |
| GH-102 | Implement `GitHubIssuesSource` reading `gh issue list` | `whilly/sources/github_issues.py` | unit test passes with mocked subprocess | 60m |
| GH-103 | CLI flag `--source gh:owner/repo[:label]` parsing | `whilly/cli.py` | `whilly --source gh:foo/bar` triggers source | 20m |
| GH-104 | Adapter writes to `tasks.json` with `source` block | `whilly/sources/github_issues.py` | written file passes `validate_schema` | 30m |
| GH-105 | Idempotent re-fetch (preserve in_progress/done by id) | `whilly/sources/github_issues.py` | unit test: re-fetch keeps `done` status | 30m |
| GH-106 | Issue closed externally → mark `skipped` | `whilly/sources/github_issues.py` | unit test | 20m |
| GH-107 | Unit tests with `subprocess.run` mocked | `tests/test_github_issues_source.py` | ≥ 75% coverage | 30m |
| GH-108 | JSONL event `source.fetch` emitted | `whilly/cli.py` | event present | 10m |

**Phase 2 total: ~3.5 hours**

### Phase 3 — PR creation sink

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| PR-201 | Create `whilly/sinks/__init__.py` package | `whilly/sinks/__init__.py` | imports clean | 5m |
| PR-202 | Implement `GitHubPRSink.open(task, worktree, agent_result)` | `whilly/sinks/github_pr.py` | unit test with mocked git+gh | 60m |
| PR-203 | PR body template with cost/duration/log link | `whilly/sinks/github_pr.py` | rendered output matches snapshot | 20m |
| PR-204 | `Closes #N` injection if `prd_requirement` is GH URL | `whilly/sinks/github_pr.py` | regex extraction tested | 20m |
| PR-205 | CLI flag `--pr-on-done` (+ env `WHILLY_PR_ON_DONE`) | `whilly/cli.py`, `whilly/config.py` | flag works | 15m |
| PR-206 | Hook PR sink into `run_plan` after task done | `whilly/cli.py` | manual smoke | 30m |
| PR-207 | Graceful failure → `sink.pr.failed` event | `whilly/sinks/github_pr.py` | unit test | 15m |
| PR-208 | `--draft` PR option | `whilly/sinks/github_pr.py`, `whilly/cli.py` | flag works | 10m |
| PR-209 | Unit tests | `tests/test_github_pr_sink.py` | ≥ 75% coverage | 30m |

**Phase 3 total: ~3.5 hours**

### Phase 4 — Decision Gate

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| DG-301 | `whilly/decision_gate.py` with `evaluate(task) -> Decision` | `whilly/decision_gate.py` | unit test | 45m |
| DG-302 | Decision dataclass `{decision, reason, cost_usd}` | `whilly/decision_gate.py` | unit test | 10m |
| DG-303 | Prompt template (RU primary) | `whilly/decision_gate.py` | snapshot test | 15m |
| DG-304 | Fail-open on timeout/parse error | `whilly/decision_gate.py` | unit test | 15m |
| DG-305 | CLI flag `--decision-gate` (+ env `WHILLY_DECISION_GATE`) | `whilly/cli.py`, `whilly/config.py` | flag works | 15m |
| DG-306 | Hook into `run_plan` between batch planning & dispatch | `whilly/cli.py` | manual smoke | 30m |
| DG-307 | On refuse + GH source → label flip via `gh issue edit` | `whilly/decision_gate.py`, `whilly/sources/github_issues.py` | manual smoke | 20m |
| DG-308 | JSONL events `decision_gate proceed/refuse` | `whilly/cli.py` | events present | 10m |
| DG-309 | Cost tracking (Decision Gate cost in budget) | `whilly/cli.py` | budget includes gate cost | 15m |
| DG-310 | Unit tests | `tests/test_decision_gate.py` | ≥ 75% coverage | 30m |

**Phase 4 total: ~3.5 hours**

### Phase 5 — Workshop UX

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| WS-501 | `examples/workshop/tasks.json` (5-10 demo tasks) | `examples/workshop/tasks.json` | passes `validate_schema` | 30m |
| WS-502 | `docs/workshop/TUTORIAL.md` (RU + EN sections) | `docs/workshop/TUTORIAL.md` | walks 90-min path | 90m |
| WS-503 | Create 5-10 GH issues in `mshegolev/whilly-orchestrator` | GitHub | issues exist with `whilly:ready` label | 20m |
| WS-504 | Add screenshots of dashboard | `docs/workshop/img/` | images present | 30m |

**Phase 5 total: ~3 hours**

### Phase 6 — README + bilingual + Obsidian sync

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| RM-601 | Add Workshop section to README.md | `README.md` | renders cleanly on GitHub | 20m |
| RM-602 | Create `README-RU.md` (краткая версия) | `README-RU.md` | bilingual coverage | 60m |
| RM-603 | `scripts/sync_workshop_docs.sh` | `scripts/sync_workshop_docs.sh` | rsync works to Obsidian path | 20m |
| RM-604 | First sync run, verify Obsidian copies | Obsidian: `02-Projects/ai/workshop/whilly/` | files present | 5m |

**Phase 6 total: ~1.5 hours**

### Phase 7 — Tests & lint

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| TST-701 | Pytest fixtures for `gh` CLI mock | `tests/conftest.py` | reusable | 30m |
| TST-702 | Run `ruff check whilly/ tests/` clean | (lint) | zero errors | 15m |
| TST-703 | Run `ruff format whilly/ tests/` | (format) | applied | 5m |
| TST-704 | `pytest -q` all green | (CI) | passing | 15m |
| TST-705 | Update CI to include new modules | `.github/workflows/ci.yml` | passes | 15m |

**Phase 7 total: ~1.5 hours**

### Phase 8 — Demo dry-run

| ID | Task | Files | Acceptance | Est |
|---|---|---|---|---|
| DR-801 | Smoke test: `whilly --source gh:... --pr-on-done --decision-gate` | (manual) | event log valid, ≥ 1 PR opened | 30m |
| DR-802 | Capture demo screencast (~3 min) | `docs/workshop/demo.gif` | embedded in README | 60m |
| DR-803 | Update READINESS-REPORT.md status row | `docs/workshop/READINESS-REPORT.md` | reflects done state | 10m |

**Phase 8 total: ~1.5 hours**

---

## Dependency graph (high level)

```
Phase 1 ──▶ Phase 2 ──┬─▶ Phase 3 ─┐
                       │            │
                       └─▶ Phase 4 ─┤
                                    ▼
                                Phase 5 ──▶ Phase 6 ──▶ Phase 8
                                    ▲
                                Phase 7 (parallel with 2-6)
```

Phase 3 and Phase 4 can run in parallel (no shared files).

---

## Total time budget

| Phase | Effort |
|---|---|
| Phase 1 (docs) | ~4h ✅ done |
| Phase 2 (GH source) | ~3.5h |
| Phase 3 (PR sink) | ~3.5h |
| Phase 4 (Decision Gate) | ~3.5h |
| Phase 5 (Workshop UX) | ~3h |
| Phase 6 (README + sync) | ~1.5h |
| Phase 7 (tests/lint) | ~1.5h |
| Phase 8 (demo) | ~1.5h |
| **Total** | **~22h** for full pack |

If solo with constrained budget (current night session):
- **Hard target:** Phase 1 ✅ + Phase 2 + Phase 3 + Phase 4 (lite) + Phase 5 (tutorial only) + Phase 7 (lint) ≈ 13 hours.
- **Stretch:** + Phase 6 + Phase 8 = full delivery.

---

## Risks per phase

| Phase | Risk | Mitigation |
|---|---|---|
| 2 | `gh` CLI version mismatch | pin minimum in TUTORIAL prerequisites |
| 3 | git push permission denied in worktree | document SSH key setup; fallback to HTTPS+token |
| 4 | Decision Gate misclassifies | conservative prompt + manual review label |
| 5 | TUTORIAL > 90 min | cut "advanced" subsections; track 90-min line strict |
| 6 | sync script overwrites Obsidian custom changes | dry-run flag + diff before copy |
| 7 | New tests slow down CI | mark slow tests `pytest.mark.slow`, skip in PR check |
| 8 | Live demo fails | pre-recorded backup, fallback to JSONL replay |

---

## Definition of done (whole gap pack)

- [ ] All Phase 2-8 tasks completed and committed.
- [ ] All AC-1 to AC-10 in PRD §4 verified.
- [ ] `pytest -q` green, `ruff check` zero errors, CI green.
- [ ] At least 1 self-hosting demo run end-to-end with PR opened.
- [ ] `docs/workshop/` synced to Obsidian.
- [ ] README workshop section published.
- [ ] Tag v3.1.0, PyPI bump.

---

**Status:** v1 · 2026-04-20 · live document, updated as phases complete.
