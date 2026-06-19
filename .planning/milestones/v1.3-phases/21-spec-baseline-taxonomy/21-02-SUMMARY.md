---
phase: 21-spec-baseline-taxonomy
plan: 02
subsystem: openspec
tags: [openspec, taxonomy, capability-index, scaffolding, kebab-case, BASE-01]

# Dependency graph
requires:
  - phase: 21-spec-baseline-taxonomy plan 01
    provides: openspec/AUTHORING.md (slug naming convention, spec format reference)

provides:
  - openspec/TAXONOMY.md — 32-capability index with slugs, one-line purposes, 6 clusters (BASE-01)
  - 32 stub directories under openspec/specs/ each with .gitkeep, names matching taxonomy slugs

affects:
  - 21-03 (task-model-fsm exemplar spec written into openspec/specs/task-model-fsm/)
  - 21-04 (coverage matrix references all 32 slugs)
  - 22-27 (every cluster phase writes spec.md into the stubs created here)
  - 28 (validation phase checks all 32 slugs have specs)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Capability taxonomy: 32 slugs across 6 clusters, one kebab-case slug per capability"
    - "Stub directory scaffold: .gitkeep per directory so empty dirs survive git"

key-files:
  created:
    - openspec/TAXONOMY.md
    - openspec/specs/orchestration-loop/.gitkeep
    - openspec/specs/task-model-fsm/.gitkeep
    - openspec/specs/plan-json-contract/.gitkeep
    - openspec/specs/batch-planning/.gitkeep
    - openspec/specs/agent-dispatch/.gitkeep
    - openspec/specs/worktree-isolation/.gitkeep
    - openspec/specs/result-collection/.gitkeep
    - openspec/specs/prd-generation/.gitkeep
    - openspec/specs/prd-wizard/.gitkeep
    - openspec/specs/task-generation/.gitkeep
    - openspec/specs/decomposition/.gitkeep
    - openspec/specs/decision-gate/.gitkeep
    - openspec/specs/jira-integration/.gitkeep
    - openspec/specs/gitlab-integration/.gitkeep
    - openspec/specs/github-integration/.gitkeep
    - openspec/specs/jira-watcher-daemon/.gitkeep
    - openspec/specs/notifications/.gitkeep
    - openspec/specs/mcp-integration/.gitkeep
    - openspec/specs/dashboard-tui/.gitkeep
    - openspec/specs/web-status-ui/.gitkeep
    - openspec/specs/reporting/.gitkeep
    - openspec/specs/cli-surface/.gitkeep
    - openspec/specs/operator-views-logs/.gitkeep
    - openspec/specs/configuration/.gitkeep
    - openspec/specs/auth-security/.gitkeep
    - openspec/specs/scheduling/.gitkeep
    - openspec/specs/state-persistence/.gitkeep
    - openspec/specs/self-update-doctor/.gitkeep
    - openspec/specs/budget-resource-guards/.gitkeep
    - openspec/specs/recovery-self-healing/.gitkeep
    - openspec/specs/quality-compliance-audit/.gitkeep
    - openspec/specs/verification-gates/.gitkeep
  modified: []

key-decisions:
  - "32 capabilities locked from 21-RESEARCH.md / REQUIREMENTS.md — no slug drift allowed"
  - ".gitkeep used in each stub directory so empty dirs survive the git commit"
  - "TAXONOMY.md lives at openspec/ root — authoritative index for all later phases"

patterns-established:
  - "Stub scaffold pattern: create directory + .gitkeep; spec.md added by capability phase"
  - "Taxonomy format: cluster heading + Markdown table with slug and one-line purpose columns"

requirements-completed: [BASE-01]

# Metrics
duration: 8min
completed: 2026-06-14
---

# Phase 21 Plan 02: Capability Taxonomy Index Summary

**32-capability taxonomy written to openspec/TAXONOMY.md across 6 clusters, with matching stub
directories under openspec/specs/ (each with .gitkeep), completing BASE-01**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-06-14T00:00:00Z
- **Completed:** 2026-06-14T00:08:00Z
- **Tasks:** 2
- **Files modified:** 33 (1 TAXONOMY.md + 32 .gitkeep files)

## Accomplishments

- Wrote `openspec/TAXONOMY.md` indexing all 32 locked capability slugs with one-line purposes,
  grouped under 6 cluster headings (Orchestration 7, PRD Pipeline 5, Integrations 6,
  Operator Surface 5, Platform 5, Safety 4), with naming convention documented.
- Created 32 stub directories under `openspec/specs/`, one per slug, each with a `.gitkeep`
  so the scaffold survives git. Directory names match taxonomy slugs exactly.
- Both grep verification gates pass: all 32 slugs present in TAXONOMY.md AND as directories.

## Task Commits

1. **Task 1: Write openspec/TAXONOMY.md capability index** - `605790f` (docs)
2. **Task 2: Create 32 capability stub directories** - `e0934e0` (chore)

## Files Created/Modified

- `openspec/TAXONOMY.md` — 32-capability index with slugs, purposes, and 6 cluster headings
- `openspec/specs/orchestration-loop/.gitkeep` — stub for Orchestration cluster capability
- `openspec/specs/task-model-fsm/.gitkeep` — stub (exemplar spec added by plan 21-03)
- `openspec/specs/plan-json-contract/.gitkeep` — stub
- `openspec/specs/batch-planning/.gitkeep` — stub
- `openspec/specs/agent-dispatch/.gitkeep` — stub
- `openspec/specs/worktree-isolation/.gitkeep` — stub
- `openspec/specs/result-collection/.gitkeep` — stub
- `openspec/specs/prd-generation/.gitkeep` — stub for PRD Pipeline cluster
- `openspec/specs/prd-wizard/.gitkeep` — stub
- `openspec/specs/task-generation/.gitkeep` — stub
- `openspec/specs/decomposition/.gitkeep` — stub
- `openspec/specs/decision-gate/.gitkeep` — stub
- `openspec/specs/jira-integration/.gitkeep` — stub for Integrations cluster
- `openspec/specs/gitlab-integration/.gitkeep` — stub
- `openspec/specs/github-integration/.gitkeep` — stub
- `openspec/specs/jira-watcher-daemon/.gitkeep` — stub
- `openspec/specs/notifications/.gitkeep` — stub
- `openspec/specs/mcp-integration/.gitkeep` — stub
- `openspec/specs/dashboard-tui/.gitkeep` — stub for Operator Surface cluster
- `openspec/specs/web-status-ui/.gitkeep` — stub
- `openspec/specs/reporting/.gitkeep` — stub
- `openspec/specs/cli-surface/.gitkeep` — stub
- `openspec/specs/operator-views-logs/.gitkeep` — stub
- `openspec/specs/configuration/.gitkeep` — stub for Platform cluster
- `openspec/specs/auth-security/.gitkeep` — stub
- `openspec/specs/scheduling/.gitkeep` — stub
- `openspec/specs/state-persistence/.gitkeep` — stub
- `openspec/specs/self-update-doctor/.gitkeep` — stub
- `openspec/specs/budget-resource-guards/.gitkeep` — stub for Safety cluster
- `openspec/specs/recovery-self-healing/.gitkeep` — stub
- `openspec/specs/quality-compliance-audit/.gitkeep` — stub
- `openspec/specs/verification-gates/.gitkeep` — stub

## Decisions Made

- Slug set copied verbatim from 21-RESEARCH.md locked taxonomy table — no additions, renames,
  or drops. Drift from this set requires an `opsx` proposal (documented in TAXONOMY.md footer).
- `.gitkeep` chosen (over empty files with other names) as the standard convention for
  empty-directory survival in git, consistent with the project's prior usage.
- TAXONOMY.md placed at `openspec/` root (not inside `specs/`) so it functions as an index
  document, separate from capability specs.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Plan 21-03 can now write `openspec/specs/task-model-fsm/spec.md` into the pre-existing stub.
- Plan 21-04 coverage matrix can reference all 32 slugs from TAXONOMY.md.
- Phases 22-27 each have their capability stub directories ready to receive `spec.md` files.
- No blockers — all 32 stubs exist, TAXONOMY.md index is authoritative.

## Self-Check

- `test -f openspec/TAXONOMY.md` — FOUND
- `ls -d openspec/specs/*/ | wc -l` — 32 FOUND
- `find openspec/specs -name .gitkeep | wc -l` — 32 FOUND
- `find openspec/specs -name spec.md | wc -l` — 0 (correct, none yet)
- All 32 slug verification loops exit 0 — PASSED
- Commits verified: 605790f, e0934e0 in git log

## Self-Check: PASSED

---
*Phase: 21-spec-baseline-taxonomy*
*Completed: 2026-06-14*
