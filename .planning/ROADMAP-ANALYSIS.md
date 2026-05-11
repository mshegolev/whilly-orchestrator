# Roadmap Replan Analysis

**Date:** 2026-05-08
**Purpose:** Reconcile the GSD roadmap with work already implemented in the repository and keep the
next phases focused on remaining gaps.

## Evidence Used

- `node /Users/m.v.shchegolev/.codex/get-shit-done/tools/gsd-tools.cjs init progress`
- `node /Users/m.v.shchegolev/.codex/get-shit-done/tools/gsd-tools.cjs roadmap analyze`
- `.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md`
- `.venv/bin/python -m whilly compliance report --format json --out out/compliance-report.json`
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
- `docs/CODEX-MISSION.md`

## Already Implemented

These items should stay closed in GSD and should not be replanned as future work:

- Operator pause/resume parity across WUI and TUI.
- Global worker pause state with local and remote worker enforcement.
- Shared WUI/API and TUI review-decision command path.
- WUI state preservation across refresh, HTMX swaps, and SSE refreshes.
- Compact WUI operator identity panel.
- Control-plane documentation framing and compliance mismatch cleanup.
- Project profiles MVP with strict loader validation and built-in profile names.
- Pipeline stage audit events for local and remote workers.
- Explicit required/optional verification command runner.
- Human-review checkpoint model with WUI/TUI controls and release-hold enforcement.
- Configured PR sink MVP, still opt-in and credential-dependent.

## Remaining Gaps

The compliance report still fails overall because some target-pack capabilities are partial or
future-only:

- Profile-native verification wiring: explicit CLI verification exists, but
  `ProjectConfig.verification_commands` still needs to feed generated plans and worker execution.
- Sandbox/secrets hardening: prompt and shell guards exist, but `a3-a4` still needs broader secret
  linting, runner environment allowlists, and auditable guard failures.
- Rollback safety net: verifier-helper rollback exists, but no general backup-tag, branch-preflight,
  and confirmation-gated restore path exists.
- CI polling and bounded repair: PR feedback polling exists, but no auditable bounded repair loop.
- Governance and semantic memory: semantic memory is not implemented; governance policy needs an
  explicit risk model or a documented deferral.

## Replan Decisions

1. Keep phases 1-4 closed as completed history.
2. Run the remaining UI work in foundation-first order: shared table contract, mobile row layout,
   then review-action affordances.
3. Move `a3-a4` sandbox/secrets hardening before profile-native verification wiring, matching
   `docs/CODEX-MISSION.md` and the recommended first cut from the doc-pack alignment roadmap.
4. Keep rollback after security hardening, then CI/repair, then governance and semantic-memory
   scope settlement.
5. Keep GSD as the canonical execution plan; superpowers docs remain detailed evidence and source
   references.

## Updated Phase Order

| Phase | Status | Focus |
|-------|--------|-------|
| 1 | Complete | Operator pause parity |
| 2 | Complete | Shared review decision path |
| 3 | Complete | WUI state-preserving refresh |
| 4 | Complete | Compact operator identity panel |
| 5 | Next | Shared operator table contract |
| 6 | Pending | Mobile WUI row actions |
| 7 | Pending | Review action affordances |
| 8 | Pending | Sandbox and secrets hardening |
| 9 | Pending | Profile-native verification wiring |
| 10 | Pending | Rollback safety net |
| 11 | Pending | CI polling and bounded repair |
| 12 | Pending | Governance and semantic-memory decision |

## Next Action

Plan Phase 5 with GSD, then execute it with tests and commit/merge before moving to Phase 6.
