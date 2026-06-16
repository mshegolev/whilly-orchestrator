# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. Completed milestone evidence is archived
under `.planning/milestones/`; `.planning/ROADMAP.md` stays small and describes only the active or
next milestone state.

## Milestones

| Milestone | Status | Shipped | Evidence |
|-----------|--------|---------|----------|
| v1.0 | Shipped | 2026-05-08 | `.planning/milestones/v1.0-ROADMAP.md`, `.planning/milestones/v1.0-REQUIREMENTS.md`, `.planning/milestones/v1.0-MILESTONE-AUDIT.md` |
| v1.1 UI parity completion | Shipped | 2026-05-11 | `.planning/milestones/v1.1-ROADMAP.md`, `.planning/milestones/v1.1-REQUIREMENTS.md`, `.planning/milestones/v1.1-MILESTONE-AUDIT.md`, `.planning/milestones/v1.1-RETROSPECTIVE.md` |
| v1.2 Adoption & live-ops | Shipped | 2026-06-12 | `.planning/milestones/v1.2-ROADMAP.md`, `.planning/milestones/v1.2-REQUIREMENTS.md`, `.planning/milestones/v1.2-MILESTONE-AUDIT.md` |
| v1.3 OpenSpec Project Baseline | Shipped | 2026-06-16 | `.planning/milestones/v1.3-ROADMAP.md`, `.planning/milestones/v1.3-REQUIREMENTS.md` |

## v1.3 OpenSpec Project Baseline — summary

Captured Whilly's guaranteed behavior as **32 normative, testable OpenSpec capability specs**
(`openspec/specs/<slug>/spec.md`), all passing `openspec validate --strict` (32/0), with a
`module → capability` coverage matrix proving **all 275 `whilly/` modules** are accounted for
(0 unmapped, 0 double-mapped). Forward delta-only process documented
(`openspec/FORWARD-PROCESS.md`); `CLAUDE.md` + `AGENTS.md` now require an `opsx` spec delta for
any behavior change. Phases 21–28, all verified. Spec-capture only — zero `whilly/` behavior
changes. Full phase detail: `.planning/milestones/v1.3-ROADMAP.md`.

## Current Milestone

None active. Start the next milestone with `/gsd-new-milestone` (defines fresh requirements +
roadmap). Future behavior changes flow through `opsx` proposals that update the relevant
capability spec (see `openspec/FORWARD-PROCESS.md`).
