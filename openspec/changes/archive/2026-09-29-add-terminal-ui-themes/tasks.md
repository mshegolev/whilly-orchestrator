- [x] Add failing preference-controller tests.
- [x] Implement shared palettes and persistent theme control without changing terminal layout.
- [x] Verify desktop/mobile browser navigation, contrast, system mode and reload persistence.
- [x] Run regressions, validate and archive the delta.

Scoped evidence: 54 Python tests and 15 JavaScript tests passed; all Jinja templates
parsed. Live Chromium checks at 1440x1000 and 390x844 verified sign-in/dashboard/
product/swarm, real Gantt bars, persistence, cross-tab/system changes, terminal
font and text/status contrast >=4.5. Three console EventSource errors were traced
to already-closed streams during dashboard navigation, not theme failures.
Independent scoped review accepted. No database/runtime configuration or paid
model calls changed; this is not a whole-repository green-suite claim.
