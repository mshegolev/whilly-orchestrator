# Proposal: Correct recovery-self-healing traceback scenario

## Why

A spec-quality audit found a LOW spec↔code inaccuracy in `recovery-self-healing`.
The "Global exception hook installation" scenario states the handler "SHALL print
the full formatted traceback afterward" unconditionally. The code
(`whilly/self_healing.py:238-250`) returns early after a successful `apply_fix`,
printing a restart notice instead of the traceback. The module is legacy/unwired,
so impact is nil, but the scenario is inaccurate.

## What Changes

- **MODIFIED** `recovery-self-healing` → "Global exception hook installation":
  traceback is printed only when no auto-fix was applied; a successful fix prints
  a restart notice and returns early. Split into two scenarios accordingly.

No `whilly/` behavior change — this aligns the spec with existing behavior.

## Impact

- Specs: `recovery-self-healing` (1 requirement, 1→2 scenarios).
- Code: none.
