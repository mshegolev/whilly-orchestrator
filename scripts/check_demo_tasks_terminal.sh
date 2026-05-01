#!/usr/bin/env bash
#
# check_demo_tasks_terminal.sh — additive exit-code guard for workshop-demo.sh.
#
# Reads `id|status` lines on stdin (one per task that is NOT in a terminal
# status: DONE / FAILED / SKIPPED) and:
#
#   * exits 0 if stdin is empty or whitespace-only (every seeded demo task has
#     reached a terminal status — workshop-demo.sh is allowed to return 0);
#   * exits 4 otherwise — printing each offending `id (status=X)` line to
#     stderr so an operator can immediately see which tasks stalled.
#
# Why a separate script: workshop-demo.sh is in the FROZEN files list. The
# guard logic is invoked from the demo script's tail (additive), and is also
# unit-testable in isolation by piping fixture stdin from
# tests/unit/test_workshop_demo_exit_code.py.
#
# Exit codes:
#   0 — every seeded task is terminal (DONE | FAILED | SKIPPED)
#   4 — at least one task is still PENDING / CLAIMED / IN_PROGRESS / unknown
#   2 — internal/usage error (currently unused; reserved)

set -euo pipefail

stuck=()
while IFS= read -r line; do
  # Strip trailing CR (psql -A output can sneak in \r on some hosts).
  line="${line%$'\r'}"
  # Skip empty / whitespace-only rows that psql -t -A occasionally emits.
  trimmed="${line//[[:space:]]/}"
  [[ -z "$trimmed" ]] && continue
  stuck+=("$line")
done

if (( ${#stuck[@]} == 0 )); then
  exit 0
fi

printf 'demo failed: %d task(s) did not reach a terminal status (DONE|FAILED|SKIPPED):\n' \
  "${#stuck[@]}" >&2
for entry in "${stuck[@]}"; do
  IFS='|' read -r tid tstatus <<<"$entry"
  printf '  - %s (status=%s)\n' "${tid:-<unknown>}" "${tstatus:-<unknown>}" >&2
done
exit 4
