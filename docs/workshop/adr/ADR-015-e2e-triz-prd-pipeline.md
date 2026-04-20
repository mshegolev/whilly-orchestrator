# ADR-015 — Whilly Wiggum e2e pipeline (TRIZ + PRD + execute)

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** self-hosting pipeline / multi-stage orchestration

## Context

The existing `whilly_e2e_demo.py` (the Ralph-loop variant) treats each GitHub
issue as a single, already-decomposed task: fetch → Decision Gate →
execute → PR → review-fix. This is fine when an issue is trivially scoped
(e.g., "add a badge to README"), but breaks down the moment an issue
describes something that deserves *thinking* before *doing*:

* refactors that span multiple files
* feature requests whose acceptance criteria aren't spelled out
* issues that contain contradictions or hidden assumptions

On a self-hosting project (whilly edits whilly), "just code it" fails
loudly — the agent produces an underspecified PR and reviewers push back.

Ghuntley's original technique doesn't prescribe what happens *between*
"pick an issue" and "hand prompt to LLM". The Whilly Wiggum framing —
Ralph's smarter brother — explicitly places TRIZ + PRD between them.

## Decision

Ship a **second** e2e script, `scripts/whilly_e2e_triz_prd.py`, alongside
the Ralph-variant. Both stay — they're complements, not alternatives:

| Script | Use when |
|---|---|
| `whilly_e2e_demo.py` | Issue is crisp, single-file, "just do it" scoped. Fast path. |
| `whilly_e2e_triz_prd.py` | Issue deserves decomposition — the "smarter brother" variant. |

Pipeline stages (per issue):

```
fetch issue
  → Decision Gate (cheap, LLM-per-issue)       ─► refuse → label flip
  → TRIZ challenge (Devil's Advocate + TRIZ)   ─► reject → label flip
  → PRD generation (issue + challenge context) ─► docs/prd/PRD-GH-N.md
  → tasks decomposition (PRD → tasks.json)     ─► whilly_GH-N_tasks.json
  → execute (whilly headless, budget-capped)   ─► commits on branch
  → quality gate (pytest + ruff)               ─► fail → `test-failed` label
  → PR (gh pr create) with body embedding:
       Challenge verdict + PRD path + tasks + gate summary
  → workflow board moves (via whilly.workflow.sync.move_on_event)
```

Key design choices:

1. **Script, not core.** Multi-stage orchestrators change faster than the
   core loop. Keeping this in `scripts/` means tightening the prompt or
   reordering stages doesn't require a whilly release.

2. **Every external effect swappable.** Every stage (`run_decision_gate`,
   `run_challenge`, `run_prd_generation`, …) is a module-level function.
   Tests monkey-patch them to verify orchestration without touching LLMs.
   This is how we ship a 9-test suite that runs in 0.1s.

3. **JSONL events for every stage.** Format matches `whilly_e2e_demo.py` —
   the eventual ADR-015-successor `ProjectBoardSyncer` will tail the same
   file and light up both pipelines for free.

4. **Board movement inline via `move_on_event`** (see
   `whilly/workflow/sync.py`). It's a synchronous no-op when
   `WHILLY_PROJECT_URL` is unset or the event is unmapped — zero-config
   path still works.

5. **Budget-wide cap.** `_total_cost` accumulates across Decision Gate +
   TRIZ + PRD + execute. When the cap is hit mid-run, remaining issues
   are skipped with `budget.exceeded` events (not errored out).

6. **Quality gate runs pytest + ruff before PR opens.** Fails label the
   PR (future) and leave it for human triage — whilly never opens a PR
   it wouldn't accept from a human contributor.

7. **No auto-merge by default.** This pipeline can modify whilly's own
   code; `--allow-auto-merge` is an explicit opt-in flag and the default
   leaves every PR for human review.

## Considered alternatives

### A. Add TRIZ+PRD stages into `whilly_e2e_demo.py`
Rejected — that script has ~700 LOC and clear semantics ("Ralph-loop
reference"). Adding 300 more LOC for a variant concept violates SRP and
makes it impossible to disable TRIZ for the trivial-issue case without
plumbing a feature flag through half the code.

### B. Generic "e2e kit" that composes stages from YAML
Overkill for v1. Two scripts is less code than one config-driven system,
and the scripts are *readable* top-to-bottom. Revisit when we have 4+
variants (the user-defined-pipelines threshold).

### C. Make TRIZ challenge a blocking Decision Gate stage in whilly core
Rejected — challenge is expensive (LLM call per issue) and only makes
sense when an issue is about to be decomposed. Core Decision Gate is
*cheap* (haiku-tier model). Merging the two would double cost on every
task for no win.

### D. Skip the quality gate; trust the agent's tests
Rejected for self-hosting. An agent modifying its own orchestrator can
break behaviour the tests can't catch (prompt regressions, event-schema
drift) — running the real test suite gives a hard signal that "whilly
still works after this change".

### E. Run every stage in a separate worktree
Considered, dropped in v1. A per-issue worktree is useful for *parallel*
issue processing and for isolating catastrophic failures. v1 is
sequential + shares the main checkout. Once we hit "two issues pipelined
concurrently" the worktree layer drops in without breaking the script
surface — the event vocabulary is already per-issue.

## Consequences

### Positive
- "Underspecified issue → reasonable PR" path exists and is opinionated.
- PRs are *traceable*: reviewer sees Challenge verdict + PRD + tasks +
  quality gate verdict in one place.
- TRIZ challenge catches scope creep / over-engineering before tokens
  are spent on implementation — cheaper than finding it in review.
- Workflow board integration "just works" when `WHILLY_PROJECT_URL` is
  set; no separate sync daemon needed in v1.

### Negative
- Two scripts now. README has to explain when to use which.
- Pipeline cost is 3-5× Ralph-loop cost per issue (cheap Gate + TRIZ +
  PRD + decomposition + execute). Budget cap keeps this bounded but
  users need to know the baseline.
- Sequential — one issue at a time. Concurrent issue processing is a
  follow-up.
- `docs/prd/PRD-GH-N.md` artefacts accumulate in the repo. We rely on
  git cleanup (or a periodic `gh issue close → squash-merge PR` cycle)
  rather than actively removing them.

### Follow-ups
- **Concurrent issue pipelining** via `.whilly_workspaces/GH-N/` worktrees.
- **Challenge output format** — v1 stuffs the raw JSON into the PRD prompt.
  A structured "TRIZ-informed scope note" section in the PRD would improve
  reviewer readability.
- **Quality gate colours** — we set board status to `failed` on both
  execution failure and quality gate failure. Distinguishing them (e.g.,
  `test-failed` vs `crashed`) would help triage.
- **Second pipeline adapter** — when Jira arrives (per ADR-014), the
  `issue_ref` format needs to route through the tracker adapter's
  naming convention, not hard-coded `owner/repo#N`.

## References

- `scripts/whilly_e2e_triz_prd.py` — the pipeline.
- `tests/test_whilly_e2e_triz_prd.py` — 9 orchestration tests, no network.
- `whilly/workflow/sync.py` — the `move_on_event` helper.
- ADR-013 — agent backends (`AgentBackend` Protocol); same idiom reused.
- ADR-014 — workflow BoardSink Protocol.
