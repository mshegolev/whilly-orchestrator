# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. Detailed historical phase plans,
validation reports, and migrated superpowers evidence are archived under `.planning/milestones/`
and `.planning/phases/`.

## Shipped Milestones

- [x] **v1.0 milestone** - Operator UI parity, safety hardening, profile-native verification,
  rollback controls, bounded CI repair, and governance scope shipped on 2026-05-08.
  Full archive: `.planning/milestones/v1.0-ROADMAP.md`.

## Current Roadmap

No active milestone is defined after v1.0. Start the next milestone with `$gsd-new-milestone` so
fresh requirements, phases, and validation gates are created from the current shipped state.

## Archives

- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

## Deferred Scope

- Live authenticated GitHub CI polling smoke against a real PR with checks.
- Browser and assistive-technology QA for the mobile WUI and review-affordance work.
- True undo semantics for review decisions.
- Full per-task VM/container isolation.
- Semantic long-term memory as a deterministic, evidence-backed capability.
