#!/usr/bin/env bash
#
# test_workshop_demo_propagates_failure_rc.sh
#
# Integration test for the cleanup_on_exit trap-rc capture pattern in
# workshop-demo.sh. The trap previously could mask a failing assertion's
# exit code with the success rc of cleanup commands (compose down, ok
# printfs, ...), making a broken demo appear green to CI.
#
# Phase 1 — STATIC ANALYSIS (always runs):
#   * `local rc=$?` is the FIRST executable line of cleanup_on_exit()
#   * `return $rc` is present inside cleanup_on_exit()'s body
#   * Trap is registered as `trap cleanup_on_exit EXIT`
#   * No `trap '' EXIT` clearer is registered later in the file
#   * WHILLY_DEMO_INJECT_FAILURE injection hook is wired
#
# Phase 2 — END-TO-END (skipped cleanly when Docker is unavailable):
#   * Run `WHILLY_DEMO_INJECT_FAILURE=min-done-999 bash workshop-demo.sh
#     --cli stub --no-color`
#   * Assert exit code is 5 (NOT 0) — proves the trap propagates rc
#   * Assert no `whilly-demo-*` containers remain — proves cleanup ran
#
# Exit codes:
#   0  — all assertions passed (or e2e was skipped cleanly)
#   1  — at least one assertion failed
#

set -euo pipefail

readonly TEST_NAME="${BASH_SOURCE[0]##*/}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly DEMO_SCRIPT="$REPO_ROOT/workshop-demo.sh"

fail() { printf '  ✗ FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf '  ✓ %s\n' "$*"; }
skip() { printf '  ~ SKIP: %s\n' "$*"; }
note() { printf '==> %s\n' "$*"; }

if [[ ! -f "$DEMO_SCRIPT" ]]; then
  fail "expected workshop-demo.sh at $DEMO_SCRIPT"
fi

# ─── Phase 1: static analysis ────────────────────────────────────────────────
note "static analysis of $(basename "$DEMO_SCRIPT")"

# `local rc=$?` is the FIRST executable line of cleanup_on_exit().
# We allow blank lines but not other code before it.
first_line_check="$(awk '
  /^cleanup_on_exit\(\)[[:space:]]*\{/ { in_func = 1; next }
  in_func {
    if ($0 ~ /^[[:space:]]*$/) next
    if ($0 ~ /^[[:space:]]*#/) next
    print $0
    exit
  }
' "$DEMO_SCRIPT")"

if [[ ! "$first_line_check" =~ ^[[:space:]]*local[[:space:]]+rc=\$\?[[:space:]]*$ ]]; then
  fail "first executable line of cleanup_on_exit() is not 'local rc=\$?'; got: ${first_line_check}"
fi
pass "cleanup_on_exit() starts with 'local rc=\$?'"

# `return $rc` is in the function body (we accept it anywhere inside, but
# it must be present and unindented-relative-to-body so it's the trap's
# explicit propagation hook).
if ! grep -qE '^[[:space:]]+return[[:space:]]+\$rc[[:space:]]*$' "$DEMO_SCRIPT"; then
  fail "cleanup_on_exit() body does not contain 'return \$rc' — trap rc propagation not wired"
fi
pass "cleanup_on_exit() contains 'return \$rc'"

# Trap is registered with EXIT semantics.
if ! grep -qE '^trap[[:space:]]+cleanup_on_exit[[:space:]]+EXIT[[:space:]]*$' "$DEMO_SCRIPT"; then
  fail "expected 'trap cleanup_on_exit EXIT' registration in $(basename "$DEMO_SCRIPT")"
fi
pass "trap cleanup_on_exit EXIT registered"

# No subsequent `trap '' EXIT` (or `trap - EXIT`) clearer that would silently
# disarm cleanup before the script's natural exit.
if grep -qE "^[[:space:]]*trap[[:space:]]+(''|\"\"|-)[[:space:]]+EXIT" "$DEMO_SCRIPT"; then
  fail "found a trap-clearer that would unregister cleanup_on_exit before exit"
fi
pass "no trap clearer disarms cleanup_on_exit"

# Failure-injection hook for the e2e probe.
if ! grep -qE 'WHILLY_DEMO_INJECT_FAILURE' "$DEMO_SCRIPT"; then
  fail "WHILLY_DEMO_INJECT_FAILURE injection hook missing"
fi
pass "WHILLY_DEMO_INJECT_FAILURE hook wired"

# ─── Phase 2: end-to-end (Docker required) ───────────────────────────────────
note "end-to-end: WHILLY_DEMO_INJECT_FAILURE=min-done-999 bash workshop-demo.sh"

if ! command -v docker >/dev/null 2>&1; then
  skip "docker not in PATH — e2e rc-propagation probe skipped"
  printf '%s: static checks passed (e2e skipped)\n' "$TEST_NAME"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  skip "docker daemon unavailable — e2e rc-propagation probe skipped"
  printf '%s: static checks passed (e2e skipped)\n' "$TEST_NAME"
  exit 0
fi

log_file="$(mktemp -t workshop-demo-rc-XXXXXX.log)"
trap 'rm -f "$log_file"' EXIT

set +e
WHILLY_DEMO_INJECT_FAILURE=min-done-999 \
  bash "$DEMO_SCRIPT" --cli stub --no-color \
  >"$log_file" 2>&1
actual_rc=$?
set -e

if (( actual_rc != 5 )); then
  printf '\n----- last 80 lines of demo output -----\n' >&2
  tail -n 80 "$log_file" >&2 || true
  printf '----- end demo output -----\n' >&2
  fail "expected demo to exit with rc=5 under WHILLY_DEMO_INJECT_FAILURE=min-done-999, got rc=${actual_rc}"
fi
pass "demo exited with rc=5 under injected failure (trap rc-capture working)"

# Cleanup invariant: docker compose down -v removes all whilly-demo-* containers.
leftover="$(docker ps -a --format '{{.Names}}' | grep -E '^whilly-demo-' || true)"
if [[ -n "$leftover" ]]; then
  fail "leftover whilly-demo-* containers after cleanup: ${leftover//$'\n'/, }"
fi
pass "no whilly-demo-* containers remain — cleanup ran"

printf '%s: all checks passed (rc=%d, cleanup verified)\n' "$TEST_NAME" "$actual_rc"
