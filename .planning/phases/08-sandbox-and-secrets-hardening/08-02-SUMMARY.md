---
phase: 08-sandbox-and-secrets-hardening
plan: 02
subsystem: security
tags: [runner-env, subprocess, secrets, claude, opencode]

requires:
  - phase: 08-sandbox-and-secrets-hardening
    provides: Phase 8 sandbox and secrets hardening scope and SEC-02 requirement.
provides:
  - Explicit coding-agent subprocess environment allowlist.
  - Provider credential inference for Claude, OpenCode, Groq, OpenAI, Gemini, OpenRouter, and OpenCode Zen models.
  - Scrubbed env wiring for Claude CLI, Claude backend, OpenCode backend, PRD generator, and handoff polling subprocesses.
affects: [phase-09-profile-native-verification, runner-backends, worker-security]

tech-stack:
  added: []
  patterns: [pure runner env builder, allowlisted subprocess env, model-based provider credential inference]

key-files:
  created:
    - whilly/adapters/runner/env.py
    - tests/unit/test_runner_env.py
  modified:
    - whilly/adapters/runner/proxy.py
    - whilly/adapters/runner/claude_cli.py
    - whilly/agents/claude.py
    - whilly/agents/opencode.py
    - whilly/agents/claude_handoff.py
    - whilly/prd_generator.py
    - tests/unit/test_claude_proxy_helpers.py
    - tests/unit/test_claude_subprocess_env.py
    - tests/test_agent_backend_claude.py
    - tests/test_agent_backend_opencode.py
    - tests/test_claude_handoff.py

key-decisions:
  - "Claude subprocess envs are built from the allowlist plus Anthropic credentials, with proxy values layered afterward only when active."
  - "OpenCode subprocess envs use the resolved model for both argv and provider credential selection, preserving zero-key opencode/big-pickle."
  - "Handoff polling subprocesses receive the allowlisted base env plus only WHILLY_HANDOFF_RESULT_PATH and WHILLY_HANDOFF_TIMEOUT."

patterns-established:
  - "Runner subprocess env construction is pure and deterministic over a supplied parent mapping."
  - "Hidden operational secrets stay out of coding-agent child processes unless explicitly required by name."

requirements-completed: [SEC-02]

duration: 7min
completed: 2026-05-08
---

# Phase 08 Plan 02: Runner Environment Allowlist Summary

**Allowlisted coding-agent subprocess environments with model-based provider credential forwarding and hidden operational secret exclusion**

## Performance

- **Duration:** 7 min
- **Started:** 2026-05-08T13:59:09Z
- **Completed:** 2026-05-08T14:06:19Z
- **Tasks:** 2
- **Files modified:** 14

## Accomplishments

- Added `whilly/adapters/runner/env.py`, a pure stdlib builder that copies only explicit base runner env names plus model-required provider credentials.
- Wired Claude proxy, async Claude CLI, sync/background Claude backend, OpenCode backend, PRD generator, and handoff polling subprocesses to pass explicit scrubbed `env=` mappings.
- Added regression coverage proving `WHILLY_DATABASE_URL`, worker/admin tokens, GitHub tokens, and Slack tokens are not forwarded to coding-agent child processes.

## Task Commits

1. **Task 1 RED: Add runner env contract tests** - `d523336` (test)
2. **Task 1 GREEN: Add pure runner env builder** - `e138f17` (feat)
3. **Task 2 RED: Add subprocess env wiring tests** - `524f76b` (test)
4. **Task 2 GREEN: Wire scrubbed runner envs** - `3248a7c` (feat)

_Note: Both plan tasks were marked `tdd="true"`, so each task has a red test commit followed by a green implementation commit._

## Files Created/Modified

- `whilly/adapters/runner/env.py` - Pure allowlist and provider credential inference contract.
- `whilly/adapters/runner/proxy.py` - Builds Claude proxy envs over scrubbed runner envs.
- `whilly/adapters/runner/claude_cli.py` - Passes model-specific scrubbed env to async Claude worker subprocesses.
- `whilly/agents/claude.py` - Passes scrubbed envs to sync and background Claude backend subprocesses.
- `whilly/agents/opencode.py` - Passes resolved-model scrubbed envs to sync and background OpenCode subprocesses.
- `whilly/agents/claude_handoff.py` - Starts handoff polling subprocesses from scrubbed envs and adds only polling-specific names.
- `whilly/prd_generator.py` - Uses model-specific Claude spawn env for PRD/task generation.
- `tests/unit/test_runner_env.py` - Covers allowlist, provider inference, deterministic ordering, and hidden-secret exclusion.
- `tests/unit/test_claude_proxy_helpers.py` - Covers inactive/active proxy env layering over scrubbed envs.
- `tests/unit/test_claude_subprocess_env.py` - Covers async Claude CLI and PRD generator env scrub behavior.
- `tests/test_agent_backend_claude.py` - Covers Claude backend `subprocess.run` and `Popen` env scrub behavior.
- `tests/test_agent_backend_opencode.py` - Covers OpenCode resolved-model env scrub and zero-key `opencode/big-pickle`.
- `tests/test_claude_handoff.py` - Covers handoff `Popen` env scrub behavior.

## Decisions Made

- Provider credential inference is based on the selected backend/model, not broad parent environment inheritance.
- `opencode/big-pickle` intentionally forwards no OpenCode provider keys so zero-key onboarding remains intact.
- Existing Claude proxy priority and probe behavior were preserved; only child env construction changed.

## Deviations from Plan

None - plan executed as written.

## Issues Encountered

- The worktree contained unrelated `.planning/STATE.md` changes and an unrelated `08-01-SUMMARY.md` from the parallel 08-01 executor. Both were left untouched per the 08-02 ownership boundary.
- The first implementation used a local `child_env` variable in `whilly/agents/claude.py`; it was tightened to the plan's explicit `env=proxy.spawn_env_for_claude(...)` grep shape before the Task 2 commit.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_runner_env.py --maxfail=1` -> `16 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_runner_env.py tests/unit/test_claude_proxy_helpers.py tests/unit/test_claude_subprocess_env.py --maxfail=1` -> `43 passed`
- `.venv/bin/python -m pytest -q tests/test_agent_backend_claude.py tests/unit/test_claude_subprocess_env.py --maxfail=1` -> `42 passed`
- `.venv/bin/python -m pytest -q tests/test_agent_backend_opencode.py --maxfail=1` -> `47 passed`
- `.venv/bin/python -m pytest -q tests/test_claude_handoff.py --maxfail=1` -> `27 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_runner_env.py tests/unit/test_claude_proxy_helpers.py tests/unit/test_claude_subprocess_env.py tests/test_agent_backend_claude.py tests/test_agent_backend_opencode.py tests/test_claude_handoff.py --maxfail=1` -> `151 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_runner_env.py tests/unit/test_claude_proxy_helpers.py tests/unit/test_claude_subprocess_env.py tests/test_agent_backend_claude.py tests/test_agent_backend_opencode.py tests/test_claude_handoff.py tests/unit/test_worker_default_deny.py --maxfail=1` -> `161 passed`
- `.venv/bin/python -m ruff check whilly/adapters/runner/env.py whilly/adapters/runner/proxy.py whilly/adapters/runner/claude_cli.py whilly/agents/claude.py whilly/agents/opencode.py whilly/agents/claude_handoff.py whilly/prd_generator.py tests/unit/test_runner_env.py tests/unit/test_claude_proxy_helpers.py tests/unit/test_claude_subprocess_env.py tests/test_agent_backend_claude.py tests/test_agent_backend_opencode.py tests/test_claude_handoff.py` -> `All checks passed`

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

SEC-02 runner env hardening is ready for later profile-native verification work. Future phases can depend on `build_runner_env()` for deterministic child environment construction instead of copying parent process envs.

## Self-Check

PASSED

- Found `whilly/adapters/runner/env.py`.
- Found `tests/unit/test_runner_env.py`.
- Found `.planning/phases/08-sandbox-and-secrets-hardening/08-02-SUMMARY.md`.
- Found task commits `d523336`, `e138f17`, `524f76b`, and `3248a7c`.
- Confirmed only unrelated `.planning/STATE.md` remains modified outside 08-02 ownership.

---
*Phase: 08-sandbox-and-secrets-hardening*
*Completed: 2026-05-08*
