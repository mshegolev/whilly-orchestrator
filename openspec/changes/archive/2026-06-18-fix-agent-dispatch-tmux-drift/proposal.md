# Proposal: Correct agent-dispatch tmux runner-selection drift

## Why

A spec-quality audit (32 capability specs vs live code) found one HIGH spec↔code
drift in `agent-dispatch`: the "Runner selection between tmux and subprocess"
requirement asserts the system selects the tmux-session runner when tmux is
available **and** `WHILLY_USE_TMUX` is enabled. The live dispatch path can never
exhibit this:

- `whilly/cli/run.py` and `whilly/cli/worker.py` wire only the subprocess runner
  `whilly.adapters.runner.run_task`; there is no tmux branch.
- No live module imports `tmux_runner.launch_agent` for selection or reads
  `WHILLY_USE_TMUX`; tmux appears only in signal-handling comments and one
  docstring.
- `WHILLY_USE_TMUX` is a documented parsed-but-inert no-op (`whilly/config.py`).

The dependent "One tmux session per task" requirement is internally accurate
against `tmux_runner.py` but only reachable via the dead selection path.

This is spec-only drift (the code is correct; the spec overstated a guarantee).
The fix corrects the spec to describe reality, mirroring how `worktree-isolation`
and `decomposition` already mark their legacy/unwired code.

## What Changes

- **MODIFIED** `agent-dispatch` → "Runner selection between tmux and subprocess":
  the live path is subprocess-only; tmux runner + `WHILLY_USE_TMUX` are
  legacy/unwired and SHALL NOT be selected.
- **MODIFIED** `agent-dispatch` → "One tmux session per task": reframed as the
  legacy `tmux_runner` contract, explicitly not wired into the live run path.

No `whilly/` behavior change — this aligns the spec with existing behavior.

## Impact

- Specs: `agent-dispatch` (2 requirements reframed).
- Code: none.
- Coverage matrix: unchanged (no module remapping).
