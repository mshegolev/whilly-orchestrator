#!/usr/bin/env bash
#
# check_demo_tasks_terminal.sh — additive exit-code guard for workshop-demo.sh.
#
# Two operating modes:
#
# 1) Default (no flags): "stuck-task" mode.
#    Reads `id|status` lines on stdin (one per task that is NOT in a terminal
#    status: DONE / FAILED / SKIPPED) and:
#      * exits 0 if stdin is empty or whitespace-only — every seeded demo
#        task has reached a terminal status (workshop-demo.sh is allowed to
#        return 0);
#      * exits 4 otherwise — printing each offending `id (status=X)` line to
#        stderr so an operator can immediately see which tasks stalled.
#
# 2) `--min-done N` (and optional `--plan SLUG`): "DONE-count" mode.
#    Reads `id|status` lines for ALL seeded tasks of the demo plan and:
#      * prints a summary on stdout in two shapes so both contracts are met:
#          - `DONE=<n> PENDING=<n> CLAIMED=<n> IN_PROGRESS=<n> FAILED=<n> SKIPPED=<n>`
#            (key=value, used by tests/integration/test_workshop_demo_drains_5_tasks.py)
#          - `<n> DONE <n> PENDING` short form (used by VAL-CROSS-BACKCOMPAT-005);
#      * exits 0 only if every row is terminal AND DONE >= N;
#      * exits 4 if any row is non-terminal (PENDING / CLAIMED / IN_PROGRESS / unknown);
#      * exits 5 if all rows are terminal but DONE < N (e.g. tasks were marked
#        FAILED / SKIPPED before reaching DONE — caught by VAL-CROSS-BACKCOMPAT-005
#        which requires 5 DONE specifically).
#
# Why a separate script: workshop-demo.sh is in the FROZEN files list. The
# guard logic is invoked from the demo script's tail (additive), and is also
# unit-testable in isolation by piping fixture stdin from
# tests/unit/test_workshop_demo_exit_code.py.
#
# Exit codes:
#   0 — every seeded task is terminal (DONE | FAILED | SKIPPED), and in
#       --min-done mode also DONE >= N
#   4 — at least one task is still PENDING / CLAIMED / IN_PROGRESS / unknown
#   5 — --min-done mode only: every task is terminal but DONE count < N
#   2 — internal/usage error (bad flag)

set -euo pipefail

MIN_DONE=""
PLAN_SLUG=""

usage_error() {
  printf 'usage: %s [--min-done N] [--plan SLUG]\n' "${0##*/}" >&2
  exit 2
}

while (( $# > 0 )); do
  case "$1" in
    --min-done)
      [[ $# -ge 2 ]] || usage_error
      MIN_DONE="$2"
      [[ "$MIN_DONE" =~ ^[0-9]+$ ]] || {
        printf 'check_demo_tasks_terminal.sh: --min-done expects a non-negative integer (got %q)\n' "$MIN_DONE" >&2
        exit 2
      }
      shift 2
      ;;
    --plan)
      [[ $# -ge 2 ]] || usage_error
      PLAN_SLUG="$2"
      shift 2
      ;;
    --help|-h)
      sed -n '1,/^set -euo/p' "$0" | grep -E '^# ?' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      printf 'check_demo_tasks_terminal.sh: unknown flag %q\n' "$1" >&2
      usage_error
      ;;
  esac
done

if [[ -n "$MIN_DONE" ]]; then
  # ── --min-done mode: receive ALL rows, count by status, assert DONE >= N ──
  done_count=0
  failed_count=0
  skipped_count=0
  pending_count=0
  claimed_count=0
  in_progress_count=0
  unknown_count=0
  unknown_lines=""

  while IFS= read -r line; do
    line="${line%$'\r'}"
    trimmed="${line//[[:space:]]/}"
    [[ -z "$trimmed" ]] && continue
    IFS='|' read -r tid tstatus <<<"$line"
    tstatus_upper="$(printf '%s' "${tstatus:-}" | tr '[:lower:]' '[:upper:]')"
    case "$tstatus_upper" in
      DONE)        done_count=$((done_count + 1)) ;;
      FAILED)      failed_count=$((failed_count + 1)) ;;
      SKIPPED)     skipped_count=$((skipped_count + 1)) ;;
      PENDING)     pending_count=$((pending_count + 1)) ;;
      CLAIMED)     claimed_count=$((claimed_count + 1)) ;;
      IN_PROGRESS) in_progress_count=$((in_progress_count + 1)) ;;
      *)
        unknown_count=$((unknown_count + 1))
        unknown_lines="${unknown_lines}${tid:-<unknown>}|${tstatus:-<unknown>}"$'\n'
        ;;
    esac
  done

  non_terminal=$((pending_count + claimed_count + in_progress_count + unknown_count))

  # Always print the summary so operators / tests can grep it.
  if [[ -n "$PLAN_SLUG" ]]; then
    printf 'plan=%s ' "$PLAN_SLUG"
  fi
  printf 'DONE=%d PENDING=%d CLAIMED=%d IN_PROGRESS=%d FAILED=%d SKIPPED=%d\n' \
    "$done_count" "$pending_count" "$claimed_count" "$in_progress_count" "$failed_count" "$skipped_count"
  printf '%d DONE %d PENDING\n' "$done_count" "$non_terminal"

  if (( non_terminal > 0 )); then
    printf 'demo failed: %d task(s) still non-terminal:\n' "$non_terminal" >&2
    if (( unknown_count > 0 )); then
      printf '%s' "$unknown_lines" | while IFS='|' read -r tid tstatus; do
        [[ -z "${tid:-}" && -z "${tstatus:-}" ]] && continue
        printf '  - %s (status=%s)\n' "${tid:-<unknown>}" "${tstatus:-<unknown>}" >&2
      done
    fi
    printf '  breakdown: PENDING=%d CLAIMED=%d IN_PROGRESS=%d unknown=%d\n' \
      "$pending_count" "$claimed_count" "$in_progress_count" "$unknown_count" >&2
    exit 4
  fi

  if (( done_count < MIN_DONE )); then
    printf 'demo failed: only %d task(s) reached DONE (required >= %d)\n' "$done_count" "$MIN_DONE" >&2
    printf '  breakdown: DONE=%d FAILED=%d SKIPPED=%d\n' "$done_count" "$failed_count" "$skipped_count" >&2
    exit 5
  fi

  exit 0
fi

# ── Default mode (legacy contract): stdin lists only non-terminal rows ──
stuck=()
while IFS= read -r line; do
  line="${line%$'\r'}"
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
