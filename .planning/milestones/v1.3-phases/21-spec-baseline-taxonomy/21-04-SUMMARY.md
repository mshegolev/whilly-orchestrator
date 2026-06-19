---
phase: 21-spec-baseline-taxonomy
plan: 04
type: summary
requirements: [BASE-02]
status: complete
---

# 21-04 SUMMARY — Module → Capability Coverage Matrix (BASE-02)

## What was built

`openspec/COVERAGE-MATRIX.md` — a single Markdown table mapping **every** `whilly/`
Python module to **exactly one** of the 32 capability slugs from `TAXONOMY.md`.

- **275 body rows = live `find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`**
  (authoritative count, computed at execution time — NOT the historical 242).
- **UNMAPPED: 0** (zero silent gaps; header-declared count matches actual).
- **Double-mapped: 0** (each module appears in exactly one row).
- **All 32 capabilities covered** (≥1 module each — no orphan capability).

## Decisions applied

- **Batch sweep**: all `whilly/adapters/db/migrations/versions/*.py` → `state-persistence`
  in one rule (generated Alembic files ARE counted, not per-file judgment).
- **Locked**: `whilly/adapters/confluence/{__init__,publisher}.py` → `notifications`
  (outbound release-doc dispatch), never UNMAPPED.
- **Primary-consumer rule** (Pitfall 6): `prd_generator.py` co-hosts `generate_prd`
  and `generate_tasks`; mapped to `prd-generation` (primary by name) with a note.
  `whilly/cli/init.py` → `task-generation` (its deliverable is `tasks.json`), so the
  `task-generation` capability has a concrete home instead of being orphaned.
- **api/ split**: auth/session/identity routes → `auth-security`; `dashboard.py` →
  `dashboard-tui`; `mailer.py` → `notifications`; remaining routes → `web-status-ui`.
- **cli/ split**: each command mapped to its domain capability (jira/gitlab/github/
  scheduler/etc.), not blanket `cli-surface`.

## Count reconciliation

The historical **242** in `REQUIREMENTS.md` is a pre-growth figure that excluded
package `__init__.py` files. It appears in the matrix header ONLY as a prose
reconciliation note; the live find count (275) is the sole row-count target.

## Verification (all gates pass)

- `grep -c '| whilly/' COVERAGE-MATRIX.md` == live find count (275). ✓
- Per-module presence loop over `find` output — no MISSING ROW. ✓
- Migrations batch-mapped to `state-persistence`. ✓
- Confluence explicitly mapped to `notifications` (non-UNMAPPED gate). ✓
- Declared UNMAPPED (0) == actual UNMAPPED (0). ✓
- No double-mapping; no blank slug cells. ✓
- Every slug is one of the 32 in `TAXONOMY.md`. ✓

## Artifacts

- `openspec/COVERAGE-MATRIX.md` (new, 317 lines: header + 275-row table).

## Phase 21 status after this plan

Plans 21-01, 21-02, 21-03, 21-04 complete → **Phase 21 (Spec Baseline & Taxonomy)
is done**. Next: `/gsd-plan-phase 22` (Orchestration Cluster — 6 remaining specs;
`task-model-fsm` already exemplar-complete).
