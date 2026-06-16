---
phase: 28-forward-process-coverage-validation
plan: 01
subsystem: openspec-process-docs
tags: [openspec, forward-process, opsx, contributor-docs, documentation-only]
requires: [v1.3 reverse-spec baseline — 32 frozen capability specs, openspec/project.md glossary (opsx, delta spec)]
provides: [openspec/FORWARD-PROCESS.md, require-a-spec-delta rule in CLAUDE.md + AGENTS.md]
affects: [openspec/project.md, openspec/AUTHORING.md, CLAUDE.md, AGENTS.md]
tech-stack:
  added: []
  patterns: [forward delta-only spec workflow (propose → apply → archive), OpenSpec=WHAT/GSD=HOW]
key-files:
  created: [openspec/FORWARD-PROCESS.md]
  modified: [openspec/project.md, openspec/AUTHORING.md, CLAUDE.md, AGENTS.md, .planning/REQUIREMENTS.md, .planning/STATE.md]
decisions:
  - "FORWARD-PROCESS.md is plain Markdown, not a capability spec — no openspec/specs/ dir, no openspec validate gate."
  - "Only real opsx/openspec surface referenced (openspec change/validate/archive, /opsx:* propose→apply→archive); no invented commands."
  - "CLAUDE.md 'should be reflected' strengthened to 'REQUIRES an opsx spec delta'; v3→v4 drift warning preserved."
metrics:
  duration: ~10m
  completed: 2026-06-16
requirements: [FWD-01, FWD-02]
---

# Phase 28 Plan 01: Forward Process & Require-a-Spec-Delta Summary

Documented the forward delta-only workflow in `openspec/FORWARD-PROCESS.md` (propose → apply → archive, OpenSpec = living WHAT / GSD = HOW) and turned the soft "should reflect the spec" guidance in CLAUDE.md/AGENTS.md into a hard requirement that any `whilly/` behavior change ship with an opsx spec delta updating `openspec/specs/<slug>/spec.md`.

## What was built

**FWD-01 — `openspec/FORWARD-PROCESS.md` (new, plain Markdown):**
- States up front: OpenSpec is the living WHAT, GSD is the HOW.
- Core rule: any change to `whilly/` runtime behavior REQUIRES an opsx change proposal that updates the relevant `openspec/specs/<slug>/spec.md`, authored as a delta spec under `openspec/changes/<name>/specs/<capability>/spec.md`, applied and archived as part of landing the change.
- Documents the three real lifecycle stages using only the confirmed opsx/OpenSpec 1.4.1 surface (`openspec change`, `openspec validate <slug> --strict`, `openspec archive`; `/opsx:*` propose→apply→archive). No invented commands.
- Boundary clarified: pure docs/test/refactor with no behavior change does not need a delta; the 32 capability specs are otherwise frozen.
- "Where things live" table points at `openspec/specs/` (baseline), `openspec/changes/` (in-flight deltas), `TAXONOMY.md`, `COVERAGE-MATRIX.md`, `AUTHORING.md`.
- Cross-referenced from `openspec/project.md` (opsx glossary entry) and `openspec/AUTHORING.md` (a callout near the top so authors hit the forward gate first).

**FWD-02 — strengthened contributor/agent rules:**
- `CLAUDE.md` "When editing": the soft "Behavior changes should be reflected in the OpenSpec capability spec" bullet is now "Behavior changes REQUIRE an opsx spec delta" — points at `openspec/specs/` and `openspec/FORWARD-PROCESS.md`; the v3→v4 drift-warning note is preserved; docs/test/refactor exemption stated.
- `AGENTS.md` (Commit & Pull Request Guidelines): added a concise rule that any `whilly/` behavior change MUST ship with an opsx spec delta updating `openspec/specs/<slug>/spec.md`, pointing at `openspec/specs/` and `FORWARD-PROCESS.md`. Existing v6/Codex content untouched.

## Deviations from Plan

None — plan executed exactly as written.

## Tasks & Commits

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | Write openspec/FORWARD-PROCESS.md (FWD-01) | 15ef1b9 | openspec/FORWARD-PROCESS.md, openspec/project.md, openspec/AUTHORING.md |
| 2 | Strengthen CLAUDE.md + AGENTS.md (FWD-02) | c37ca27 | CLAUDE.md, AGENTS.md |

## Verification

- Task 1 automated verify: `PASS` (FORWARD-PROCESS.md exists; contains propose/apply/archive + `openspec/specs/`; referenced from project.md + AUTHORING.md). Confirmed no `openspec/specs/forward-process/` dir created.
- Task 2 automated verify: `PASS` (CLAUDE.md has require + `openspec/specs/` + FORWARD-PROCESS; AGENTS.md has `openspec/specs/` + opsx/spec-delta + FORWARD-PROCESS).
- No `whilly/` Python files modified (documentation-only).

## Self-Check: PASSED

- FOUND: openspec/FORWARD-PROCESS.md
- FOUND: commit 15ef1b9
- FOUND: commit c37ca27
