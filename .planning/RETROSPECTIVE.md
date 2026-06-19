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

## Milestone: v1.5 — Semantic Drift-Guard

**Shipped:** 2026-06-19
**Phases:** 4 (30–33) | **Plans:** 5 | **Commits:** 34

### What Was Built
An agent-assisted semantic spec-fidelity checker (`scripts/semantic_drift_check.py` + scheduled
`semantic-drift.yml`): single-spec engine → bounded/resilient 6-cluster fan-out over all 32 specs →
scheduled CI job with `--fail-on {none,high}` gating → known-drift fixture validation. Catches the
drift class v1.4's mechanical gate cannot — a spec whose `SHALL` still validates while the code
diverged.

### What Worked
- The milestone was seeded by a real finding: a manual 6-cluster audit (run live this session) found
  1 HIGH + 3 MEDIUM real bugs the mechanical gate missed. v1.5 automated exactly that proven shape.
- The injected-reviewer seam made an LLM tool deterministically testable — 60+ offline tests + a
  skip-when-no-`claude` live canary. Every phase verified green without flakiness.
- Placing the tool in `scripts/` (not `whilly/`) sidestepped a recursive spec obligation and kept
  the milestone at zero behavior change — the OpenSpec end-wing re-confirmed 32/0 + 275/275 intact.
- The live canary actually ran against the real model and confirmed drifted→HIGH / clean→clean —
  real trustworthiness proof, not a mock.

### What Was Inefficient
- The orchestrator context grew large across phases; the GSD-native fix (one `/clear` + re-run
  `/gsd-autonomous` per phase, resuming from disk state) kept quality high but required operator
  re-invocation between phases.
- Phase executors didn't always run the STATE/ROADMAP/REQUIREMENTS SDK updates (out of plan scope),
  so the orchestrator flipped requirement checkboxes + traceability at each phase close.

### Patterns Established
- **LLM-tool testability:** inject the model call as a `Callable[[str], str]`; default shells to the
  CLI, tests pass a fake. Deterministic plumbing test + skip-guarded live canary.
- **Self-validating guard:** ship a known-drift fixture so the detector proves itself, re-runnable
  in scheduled CI as a canary.
- **Tooling-outside-package** to avoid self-referential spec/coverage obligations.

### Key Lessons
- A mechanical gate proves *coverage + structure*; it cannot prove *semantic fidelity*. The two are
  complementary, not redundant — v1.5 exists precisely because v1.4 couldn't see meaning drift.
- Build the detector against a *known* finding first (the manual audit), so "does it actually work"
  has a concrete yes/no answer.

### Cost Observations
- Model mix: orchestration on opus; planners opus, executors/checkers/verifiers sonnet.
- Sessions: phase-isolated (one fresh context per phase, GSD-recommended).
- Notable: the live canary (~3–4 min real-model run) is the costly step — correctly gated to
  scheduled CI, not per-PR.

## Cross-Milestone Trends

| Milestone | Phases | Plans | Deferred validation at close |
|-----------|--------|-------|------------------------------|
| v1.0 | 12 | 25 | 2 items (CI smoke, browser QA) |
| v1.1 | 7 | 12 | 3 items (live Jira/GitLab smoke, Alembic chain, watcher) |
| v1.2 | 3 | 9 | 0 items — all validated live at close |
| v1.5 | 4 | 5 | 0 items — 2 audit tech-debt items fixed before close; live canary validated |

Trend: v1.2 closed the validation debt of both prior milestones; keep "validate live at close"
as the default bar. v1.5 held the bar — audit tech-debt fixed inline and the live model canary
ran before close rather than being deferred.
