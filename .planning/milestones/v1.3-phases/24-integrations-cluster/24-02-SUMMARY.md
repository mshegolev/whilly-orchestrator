---
phase: 24-integrations-cluster
plan: "02"
subsystem: integrations
tags: [github, openspec, capability-spec, auth, reverse-spec, subsystem-altitude]

requires:
  - phase: 21-spec-baseline
    provides: openspec/AUTHORING.md conventions + task-model-fsm exemplar
  - phase: 24-integrations-cluster
    provides: read-only-vs-mutating + auth-expectations spec pattern (24-01)
provides:
  - openspec/specs/github-integration/spec.md (INT-03) — GitHub subsystem contract at subsystem altitude (auth, read/mutating boundary, PR sink, projects sync, issue→plan conversion/intake, sources, workflow engine, CI poll)
affects: [24-integrations-cluster, verifier]

tech-stack:
  added: []
  patterns:
    - "Reverse-spec broad subsystem (32 modules) at subsystem-contract altitude — one requirement per sub-surface, not per module"
    - "Centralised auth helper (gh_subprocess_env) specced as a shared cross-module contract"
    - "Read-only vs single-set-of-mutating-paths boundary stated as an explicit requirement"

key-files:
  created:
    - openspec/specs/github-integration/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md

key-decisions:
  - "7 requirements at sub-surface altitude (auth, reads, idempotent merge, conversion+intake, mutating boundary, workflow Protocol, CI poll) — per-module accounting left to coverage matrix"
  - "Mutating boundary enumerated exactly: PR create, project status write, issue create, forge label flip — each specced never-raise/structured-failure"
  - "Normative SHALL/MUST forced onto the literal first body line (validator only checks first body line; wrapped leading clauses failed initially)"

patterns-established:
  - "Pattern: broad subsystems get sub-surface requirements grouped by behavior contract, never one-per-module"

requirements-completed: [INT-03]

duration: 14min
completed: 2026-06-16
---

# Phase 24 Plan 02: GitHub Integration Subsystem Spec Summary

**One reverse-spec'd OpenSpec capability spec — github-integration (INT-03) — capturing the 32-module GitHub subsystem at subsystem-contract altitude across 7 sub-surfaces (gh auth resolution chain, read-only reads, idempotent issue→plan merge with secret detection, deterministic conversion + idempotent Forge intake, the confined mutating boundary, the pluggable BoardSink workflow Protocol, and the never-raising CI poll adapter), passing `openspec validate --strict` clean.**

## Performance

- **Duration:** ~14 min
- **Started:** 2026-06-16
- **Completed:** 2026-06-16
- **Tasks:** 1
- **Files modified:** 3 (1 spec created, REQUIREMENTS.md + STATE.md updated)

## Accomplishments
- INT-03 `github-integration` spec written at subsystem altitude with 7 requirements, each grounded in observed v4 code:
  1. **Centralised gh CLI auth resolution** — the `gh_subprocess_env` token chain (WHILLY_GH_TOKEN → WHILLY_GH_PREFER_KEYRING strip → `[github].token` from whilly.toml → ambient passthrough), with scenarios for each branch.
  2. **Read-only GitHub state reads** — issue sources, `fetch_github_issues`, project item fetch, and the CI poll adapter read only, never mutate.
  3. **Idempotent issue→plan merge with secret detection** — `merge_into_plan` preserves status, refreshes mutable fields, skips externally-closed `GH-` tasks, and flags secret-pattern matches.
  4. **Deterministic conversion + idempotent Forge intake** — `convert_issues_to_tasks` is OPEN-only and deterministic; `whilly forge intake owner/repo/<N>` re-run returns the existing plan id, exits 0, burns no Claude tokens, and does not re-flip the label; label flip is the last step after the DB commit.
  5. **Mutating boundary confined** — PR create (`open_pr_for_task`), project status write (`sync_status_changes`), issue create (`_create_github_issue`), and the forge label flip are the only mutating paths; each surfaces a structured failure rather than crashing the loop (`PRResult.failure_mode`, `False`, or logged warning); existing-PR is treated as success.
  6. **Pluggable board-workflow contract** — `BoardSink` Protocol + `LifecycleEvent` + `WorkflowMapping`, resolved via `get_board`/`available_boards`; `move_item` returns `False` on transport error, never raises; unknown board name → `ValueError`.
  7. **CI poll adapter explicit evidence** — `GitHubCIPollAdapter` runs one bounded read-only `gh pr view` probe returning unauthenticated/unavailable/timed-out/rollup outcomes, never raising; unparseable target returns unavailable without invoking `gh`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Spec the observed v4 behavior of the GitHub subsystem at contract altitude (INT-03)** — `979ec1d` (docs)

**Plan metadata:** committed with this SUMMARY (docs: complete plan)

## Files Created/Modified
- `openspec/specs/github-integration/spec.md` — INT-03 normative subsystem-altitude spec reverse-spec'd from `whilly/gh_utils.py`, `whilly/sinks/github_pr.py`, `whilly/github_projects.py`, `whilly/github_converter.py`, `whilly/forge/intake.py`, `whilly/sources/github_issues.py`, `whilly/workflow/__init__.py` + `base.py`, `whilly/ci/github.py`
- `.planning/REQUIREMENTS.md` — INT-03 checked off; traceability row added
- `.planning/STATE.md` — Current Position advanced to Phase 24 / Plan 24-02

## Decisions Made
- Kept the spec at sub-surface altitude (7 requirements) rather than one requirement per module, per the CONTEXT note that the coverage matrix carries per-module accounting for this broad capability.
- Enumerated the mutating boundary exactly — PR create, project status write, issue create, forge label flip — naming each path from source rather than a generic "writes are mutating" statement, and specced the never-raise/structured-failure discipline observed in each (`PRResult.failure_mode`, `sync_status_changes` returning `False`, forge label-flip warning-and-exit-0).
- Stated the auth chain as a shared cross-module contract anchored on `gh_subprocess_env`, since every `gh` subprocess across the subsystem routes through it.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Normative keyword not on first body line (2 requirements)**
- **Found during:** Task 1 (first strict validation run)
- **Issue:** Requirements "Read-only GitHub state reads" and "Deterministic issue→task conversion and idempotent Forge intake" opened with a descriptive subject clause that pushed `SHALL`/`MUST` onto a later wrapped line. The validator checks only the first non-empty body line, producing `ERROR: Requirement must contain SHALL or MUST keyword` for both.
- **Fix:** Reworded both so the normative `SHALL`/`MUST` clause is the literal first body line; descriptive enumeration moved to following sentences.
- **Files modified:** openspec/specs/github-integration/spec.md
- **Commit:** 979ec1d (fixed before the task commit)

After the fix, `openspec validate github-integration --strict` reported the spec is valid (0 errors, 0 warnings, exit 0).

## Issues Encountered
None beyond the first-body-line validator nuance documented above.

## Known Stubs
None — the file is a complete normative spec grounded in real v4 code.

## User Setup Required
None — documentation only; zero `whilly/` changes.

## Next Phase Readiness
- INT-03 specced and validated. Remaining Phase 24 work: INT-02 (gitlab-integration), INT-05 (notifications), INT-06 (mcp-integration).
- No blockers. The subsystem-altitude grouping pattern established here applies to any future broad-capability specs.

## Self-Check: PASSED
- openspec/specs/github-integration/spec.md — FOUND
- Commit 979ec1d — FOUND in git log
- `openspec validate github-integration --strict` — exit 0, "is valid"

---
*Phase: 24-integrations-cluster*
*Completed: 2026-06-16*
