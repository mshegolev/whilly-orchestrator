---
phase: 21-spec-baseline-taxonomy
plan: "01"
subsystem: openspec
tags: [documentation, openspec, authoring-conventions, project-context, BASE-03, BASE-04]

dependency_graph:
  requires: []
  provides:
    - openspec/AUTHORING.md (spec authoring conventions — BASE-03)
    - openspec/project.md (Whilly project context — BASE-04)
    - openspec/config.yaml context: pointer to project.md
  affects:
    - All phase 22-27 spec authors (format reference)
    - openspec validate --strict (docs describe its exact rules)

tech_stack:
  added: []
  patterns:
    - OpenSpec 1.4.1 spec-driven schema (documented, not changed)
    - RFC 2119 normative language (SHALL/MUST convention)

key_files:
  created:
    - openspec/AUTHORING.md
    - openspec/project.md
  modified:
    - openspec/config.yaml

decisions:
  - "openspec/project.md is the primary context document; config.yaml context:
    field holds a 6-line pointer summary — both populated per plan spec"
  - "AUTHORING.md anti-patterns section covers all 8 verified forbidden patterns
    from 21-RESEARCH.md live probe tests"
  - "config.yaml context: uses YAML block scalar (|) to preserve multi-line
    content including the <promise>COMPLETE</promise> signal"

metrics:
  duration: "~15 minutes"
  completed: "2026-06-14"
  tasks_completed: 2
  tasks_total: 2
  files_created: 2
  files_modified: 1
---

# Phase 21 Plan 01: Spec Baseline — Authoring Conventions & Project Context

One-liner: Locked OpenSpec 1.4.1 spec format in AUTHORING.md and Whilly's
WHILLY_ env-var / exit-code / FSM / completion-signal contracts in project.md.

---

## Tasks Completed

| # | Task | Commit | Files |
|---|------|--------|-------|
| 1 | Write openspec/AUTHORING.md (BASE-03) | `4e10778` | `openspec/AUTHORING.md` (created) |
| 2 | Write openspec/project.md + wire config.yaml (BASE-04) | `5464584` | `openspec/project.md` (created), `openspec/config.yaml` (modified) |

---

## What Was Built

### openspec/AUTHORING.md (BASE-03)

Canonical spec authoring guide for all `openspec/specs/` capability specs.
Documents the full OpenSpec 1.4.1 strict format sourced from 21-RESEARCH.md
verified facts (read from installed CLI source + 12 live probe tests):

- Spec file location pattern and slug naming convention (kebab-case)
- Two required `##` sections: `## Purpose` (>=50 chars) and `## Requirements`
- Requirement block format: `### Requirement:` + first-body-line `SHALL`/`MUST`
  + `#### Scenario:` (exactly 4 hashtags) + `WHEN`/`THEN` bullets
- Strict validation checklist with all ERROR/WARNING failure modes in a table
- Anti-patterns section covering all 6 forbidden patterns: delta headers in main
  specs, SHALL/MUST only in header, bullet scenarios, 3-hashtag scenario headers,
  descriptive language, and requirements with 0 scenarios
- Validation commands reference and RFC 2119 normative language table
- Authoring quick-reference checklist (7 checkbox items)

### openspec/project.md (BASE-04)

Whilly project context document for OpenSpec spec authors:

- Tech stack table: Python 3.10+, Rich, FastAPI, SQLAlchemy, ruff, pytest
- Full WHILLY_ env-var contract table (12 variables with defaults)
- Task status FSM: 5 legal values, transition rules, stale-reset-on-startup,
  terminal state immutability
- Completion signal: `<promise>COMPLETE</promise>` (exact literal)
- Exit codes: 0=ok / 1=some failed / 2=budget exceeded / 3=timeout
- Plan JSON envelope: `{project, prd_file, tasks: [...]}` with all 8 task fields
- Retry and deadlock policy (exponential backoff, MAX_TASK_RETRIES, 3-iteration
  deadlock detection)
- Domain glossary: 14 terms (capability, plan, task, agent, workspace, worktree,
  tmux session, PRD, Decision Gate, TRIZ, StateStore, opsx, delta spec,
  coverage matrix)
- Normative language convention table

### openspec/config.yaml (context: field)

Populated the previously empty `context:` field with a 6-line YAML block scalar
that summarizes the stack and explicitly points AI tooling to `openspec/project.md`
(full glossary) and `openspec/AUTHORING.md` (authoring rules).

---

## Verification Results

All acceptance criteria passed:

**Task 1:**
- `test -f openspec/AUTHORING.md` — PASS
- `grep -c 'SHALL' openspec/AUTHORING.md` = 17 (>= 1) — PASS
- `grep -q '#### Scenario:' openspec/AUTHORING.md` — PASS
- `grep -q -- '--strict' openspec/AUTHORING.md` — PASS
- `grep -qi 'ADDED Requirements' openspec/AUTHORING.md` — PASS
- `grep -qE '>= 50|>=50|50 character' openspec/AUTHORING.md` — PASS

**Task 2:**
- `test -f openspec/project.md` — PASS
- `grep -q 'WHILLY_' openspec/project.md` — PASS
- `grep -q 'promise>COMPLETE' openspec/project.md` — PASS
- `grep -qE 'exit (code|codes)' openspec/project.md` — PASS
- `grep -q 'pending' ... && grep -q 'in_progress'` — PASS
- `grep -q 'StateStore' ... && grep -q 'TRIZ'` — PASS
- `grep -q 'project.md' openspec/config.yaml` — PASS
- No secrets leaked — PASS

**Plan-level verification:**
- `openspec validate --specs --strict --json` runs without crash, returns
  empty result (0 specs — expected, this plan adds docs not specs) — PASS

---

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Case-sensitive exit-code grep match**
- **Found during:** Task 2 acceptance criteria verification
- **Issue:** The plan's grep `grep -qE 'exit (code|codes)'` is case-sensitive.
  The initial project.md used "Exit Code Contract" (capital letters) as the
  section heading, causing the grep to miss the match.
- **Fix:** Added an explicit lowercase phrase "Whilly uses four exit codes for
  machine-readable completion signaling:" before the table in the Exit Code
  Contract section.
- **Files modified:** `openspec/project.md`
- **Impact:** None to content; one sentence added for grep compliance.

---

## Known Stubs

None. Both documents are complete authoring guides, not stubs. No placeholder
text or TODO items were left in the created files.

---

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes
introduced. This plan created documentation files only (`openspec/AUTHORING.md`,
`openspec/project.md`, `openspec/config.yaml` context field). The threat model
mitigations verified:

- T-21-01 (Information Disclosure): `grep -rIl 'STRIPE|GITLAB_TOKEN|
  PRIVATE-TOKEN|password'` returned no matches — MITIGATED.

---

## Self-Check: PASSED

- `openspec/AUTHORING.md` — FOUND
- `openspec/project.md` — FOUND
- Commit `4e10778` (AUTHORING.md) — FOUND in `git log`
- Commit `5464584` (project.md + config.yaml) — FOUND in `git log`
