# Project Retrospective — Whilly Orchestrator

Living document. One section per shipped milestone, appended at completion.
(v1.1 has a standalone archive: `.planning/milestones/v1.1-RETROSPECTIVE.md`.)

## Milestone: v1.2 — Adoption & live-ops

**Shipped:** 2026-06-12
**Phases:** 3 (18–20) | **Plans:** 9

### What Was Built

- Phase 18: Full Alembic chain (001→028) validated from empty Docker Postgres; `make
  migrate-chain`; green `migration-chain` CI job with evidence artifact.
- Phase 19: `whilly jira smoke` + new `whilly gitlab smoke` on a shared SmokeReport foundation
  with redacted persisted reports; validated live against corporate Jira/GitLab.
- Phase 20: `whilly jira watch` daemon — interval polling, graceful stop, status file +
  `watch-status`, PID guard, backoff audit events, fail-closed pause/readiness gates,
  default-off dispatch; validated live.

### What Worked

- **Live validation as UAT.** Running the shipped commands against real Jira/GitLab/CI closed
  every deferred "human verification" item same-day and surfaced a real environment fact
  (Jira Server/DC needs `JIRA_AUTH_SCHEME=bearer` + `JIRA_API_VERSION=2`) that became a doc fix.
- **Review→fix→falsify loop.** Each phase's code review ran before verification; all Critical
  findings were fixed with regression tests that actively try to falsify the fix (e.g., partial
  test runs proving evidence flags go false).
- **Pattern mapper.** Concrete file:line analogs (smoke → watch, state_store → status file)
  made executor output land in-convention on the first pass.
- **Phase 19 lesson reuse.** The `--interactive-config` AttributeError pitfall was recorded in
  STATE.md and explicitly carried into Phase 20 planning — it did not recur.

### What Was Inefficient

- The fabricated-evidence bug class (pass flags as literals) was written by executors in ALL
  THREE phases despite being fixed in the previous one — planning now needs an explicit
  "honest accumulation" acceptance criterion up front, not just review-time catches.
- Phase 20's production dispatch closure shipped untested (every test injected a fake runner)
  — caught by review, but a "test the real seam construction" rule in plans would be cheaper.
- Auto-extracted milestone accomplishments grabbed literal "One-liner:" labels from two
  summaries — summary frontmatter discipline varies between executors.

### Patterns Established

- Shared smoke/report foundation (`whilly/cli/smoke.py`): honest per-check accumulation,
  secret-redacting report writer, exit codes 0/1/2.
- Optional-DB persistence: best-effort, warn-not-fail, never a hard dependency.
- Live-validation UAT recorded in `*-HUMAN-UAT.md` with command transcripts as evidence.

### Key Lessons

1. Make "no fabricated values" an explicit planner-level acceptance criterion for any
   evidence/status artifact.
2. Any injectable seam needs at least one test that exercises the *production* wiring.
3. Corporate Jira is Server/DC: api/2 + Bearer PAT; api/3 returns HTML login (HTTP 200/302) —
   detect HTML responses as a failure class (already implemented in smoke).

### Cost Observations

- Model mix: planner on opus; researcher/checker/executor/verifier on sonnet.
- Sessions: single autonomous session end-to-end (milestone init → audit → archive).
- Notable: live UAT (smoke + watcher + CI push) cost minutes and eliminated all deferred
  validation debt that v1.0/v1.1 had accumulated at close.

## Cross-Milestone Trends

| Milestone | Phases | Plans | Deferred validation at close |
|-----------|--------|-------|------------------------------|
| v1.0 | 12 | 25 | 2 items (CI smoke, browser QA) |
| v1.1 | 7 | 12 | 3 items (live Jira/GitLab smoke, Alembic chain, watcher) |
| v1.2 | 3 | 9 | 0 items — all validated live at close |

Trend: v1.2 closed the validation debt of both prior milestones; keep "validate live at close"
as the default bar.
