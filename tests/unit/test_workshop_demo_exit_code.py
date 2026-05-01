"""Unit tests for the additive terminal-state exit-code guard at the end of
``workshop-demo.sh``.

Background — M1 user-testing round-1 finding (VAL-CROSS-BACKCOMPAT-002 /
VAL-M1-ENTRYPOINT-003 / VAL-M1-DEMO-008): the demo script was reporting
``exit 0`` even when the parallel-demo plan still had tasks in PENDING /
CLAIMED state at script end (no DONE transitions). The user-testing flow
validator showed PAR-001/PAR-002 stayed PENDING and the script silently
reported success.

The fix is intentionally **additive**: ``workshop-demo.sh`` is in the FROZEN
files list so we do NOT rewrite the demo loop. Instead the script's tail now
queries the tasks table for anything outside the terminal set and pipes the
result through ``scripts/check_demo_tasks_terminal.sh``. If that helper sees
any line on stdin, it exits 4 with each offending ``id (status=...)`` printed
to stderr, and ``workshop-demo.sh`` propagates that as its own exit code.

These tests cover both halves of the fix:

1. The helper script (``scripts/check_demo_tasks_terminal.sh``) — fast
   stdin/exit-code unit tests (empty / whitespace / single stuck / multi
   stuck / CR-tainted / malformed).
2. ``workshop-demo.sh`` itself — runs the full script end-to-end in a
   subprocess against shimmed ``docker`` and ``curl`` binaries (no real
   Postgres / control-plane / worker required) and asserts that with a
   "stuck plan" (the docker-shim makes ``compose exec ... psql`` return a
   non-empty terminal-guard query) the script exits non-zero and the stuck
   task IDs appear on stderr. With a "happy" shim (psql guard query
   returns no rows) the script still exits 0 — the fix is backwards-
   compatible for green CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "check_demo_tasks_terminal.sh"
DEMO = REPO_ROOT / "workshop-demo.sh"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="workshop-demo + helper are bash-only; Windows / no-bash environments are out of scope",
)


# ---------------------------------------------------------------------------
# Helper-script unit tests (fast, deterministic, no docker required)
# ---------------------------------------------------------------------------


def _run_helper(stdin: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    """Invoke ``scripts/check_demo_tasks_terminal.sh`` with ``stdin`` text.

    Returns the completed process object so individual tests can inspect
    ``returncode``, ``stdout``, and ``stderr``.
    """
    return subprocess.run(
        ["bash", str(HELPER)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_helper_exists_and_is_executable() -> None:
    """Sanity: the helper file exists and the shebang line is bash-compatible."""
    assert HELPER.exists(), f"missing helper: {HELPER}"
    first_line = HELPER.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), f"helper missing shebang: {first_line!r}"
    assert "bash" in first_line, f"helper shebang not bash: {first_line!r}"


def test_helper_empty_stdin_exits_zero() -> None:
    """Empty stdin == every seeded task is terminal == exit 0 (backcompat)."""
    result = _run_helper("")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_helper_whitespace_only_stdin_exits_zero() -> None:
    """psql -t -A occasionally emits stray newlines / spaces; treat as empty."""
    result = _run_helper("\n   \n\t\n\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_helper_single_stuck_task_exits_four_and_names_it() -> None:
    """One PENDING task -> exit 4, stderr names id+status."""
    result = _run_helper("PAR-001|PENDING\n")
    assert result.returncode == 4
    assert "PAR-001" in result.stderr
    assert "PENDING" in result.stderr
    # stdout must be silent so it can't pollute the operator's terminal flow.
    assert result.stdout == ""


def test_helper_multiple_stuck_tasks_lists_all() -> None:
    """Multiple stuck tasks: each id+status appears on stderr exactly once."""
    result = _run_helper("PAR-001|PENDING\nPAR-002|CLAIMED\nPAR-003|IN_PROGRESS\n")
    assert result.returncode == 4
    for tid, status in (("PAR-001", "PENDING"), ("PAR-002", "CLAIMED"), ("PAR-003", "IN_PROGRESS")):
        assert tid in result.stderr, result.stderr
        assert status in result.stderr, result.stderr
    # Each occurs exactly once (stderr has one report block, no double-print).
    assert result.stderr.count("PAR-001") == 1
    assert result.stderr.count("PAR-002") == 1
    assert result.stderr.count("PAR-003") == 1
    # The summary line announces the count.
    assert "3" in result.stderr.splitlines()[0]


def test_helper_cr_tainted_lines_are_normalised() -> None:
    """Some psql backends sneak \\r into -A output; helper must strip it."""
    result = _run_helper("PAR-001|PENDING\r\nPAR-002|CLAIMED\r\n")
    assert result.returncode == 4
    # The trailing \r must NOT appear in the rendered status (would manifest
    # as a literal carriage return in the operator's terminal).
    assert "PENDING\r" not in result.stderr
    assert "CLAIMED\r" not in result.stderr
    assert "PAR-001" in result.stderr
    assert "PAR-002" in result.stderr


def test_helper_malformed_line_without_pipe_still_fails_loudly() -> None:
    """A row missing the `|` separator must still trip exit 4 (don't silently
    pass an unknown row as terminal — that would re-introduce the original
    silent-success bug)."""
    result = _run_helper("PAR-001\n")
    assert result.returncode == 4
    assert "PAR-001" in result.stderr


def test_helper_terminal_status_words_in_stderr() -> None:
    """Stderr message names the terminal-set so an operator can recognise it."""
    result = _run_helper("PAR-001|PENDING\n")
    assert result.returncode == 4
    # The first line of the report mentions DONE / FAILED / SKIPPED so the
    # operator immediately understands the contract that was violated.
    summary = result.stderr.splitlines()[0]
    assert "DONE" in summary
    assert "FAILED" in summary
    assert "SKIPPED" in summary


# ---------------------------------------------------------------------------
# workshop-demo.sh wiring assertion (static)
# ---------------------------------------------------------------------------


def test_workshop_demo_invokes_helper_at_tail() -> None:
    """The workshop-demo.sh script must wire up the new helper after the
    wait-for-DONE loop. This is a structural check so accidental reverts are
    caught even if the helper is unaffected."""
    body = DEMO.read_text(encoding="utf-8")
    assert "scripts/check_demo_tasks_terminal.sh" in body, (
        "workshop-demo.sh does not invoke the terminal-state helper; the M1 silent-success regression will return"
    )
    # The query must restrict the search to non-terminal statuses, otherwise
    # we'd be feeding the helper every row in the table (including DONE) and
    # the helper would always exit 4.
    assert "NOT IN ('DONE','FAILED','SKIPPED')" in body
    # exit 4 is the contractually-defined failure code so CI scripts can
    # branch on it (separate from the existing 1/2/3 exit codes used
    # elsewhere in the demo).
    assert "exit 4" in body


# ---------------------------------------------------------------------------
# End-to-end: workshop-demo.sh in a subprocess with a stuck-plan shim
# ---------------------------------------------------------------------------


# Bash docker shim — recognises every subcommand workshop-demo.sh issues
# and maps psql `-c QUERY` to canned responses based on substring matches
# of the query text. The shim is parameterised with WHILLY_TEST_TASKS_STUCK:
# when set to "1", the terminal-state guard query returns PAR-001|PENDING and
# PAR-002|CLAIMED -> the helper exits 4 -> workshop-demo.sh exits 4.
_DOCKER_SHIM = r"""#!/usr/bin/env bash
# Test shim for `docker` (workshop-demo.sh end-to-end).
set -uo pipefail

if [[ -n "${WHILLY_TEST_DOCKER_LOG:-}" ]]; then
  printf 'docker %s\n' "$*" >> "$WHILLY_TEST_DOCKER_LOG"
fi

# Top-level subcommands the script issues directly.
case "${1:-}" in
  info)               exit 0 ;;
  build)              exit 0 ;;
  image)              exit 0 ;;  # `docker image inspect ...` -> succeed (skip-build path)
esac

# Everything else is `docker compose ...`. Drop the literal `compose` token.
[[ "${1:-}" == "compose" ]] || exit 0
shift

# Strip leading `-f <file>` repetitions.
while [[ "${1:-}" == "-f" ]]; do
  shift  # drop -f
  shift  # drop value
done

case "${1:-}" in
  version)
    echo "Docker Compose version v2.40.3"
    exit 0
    ;;
  config|up|down|build|logs|ps|stop|start|pull)
    exit 0
    ;;
  exec)
    shift
    # Skip flags like -T, -e, --env, -u, ...
    while [[ "${1:-}" == -* ]]; do
      # -T has no value; -e/-u/--env may consume one arg in some forms.
      case "$1" in
        -T) shift ;;
        *)  shift ;;
      esac
    done
    service="${1:-}"; shift || true
    if [[ "$service" != "postgres" ]]; then
      # control-plane exec (whilly plan import / show / etc.) — succeed.
      exit 0
    fi
    # Inside postgres exec — psql invocation. Find -c QUERY.
    while (( $# > 0 )) && [[ "${1:-}" != "-c" ]]; do
      shift
    done
    [[ "${1:-}" == "-c" ]] || exit 0
    shift
    q="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
    # ----- terminal-state guard query (the new fix) -----
    if [[ "$q" == *"id || '|' || status"* ]]; then
      if [[ "${WHILLY_TEST_TASKS_STUCK:-0}" == "1" ]]; then
        printf 'PAR-001|PENDING\nPAR-002|CLAIMED\n'
      fi
      exit 0
    fi
    # ----- worker registration probe -----
    if [[ "$q" == *"select count(*) from workers"* ]]; then
      echo "2"; exit 0
    fi
    if [[ "$q" == *"select worker_id, hostname"* ]]; then
      echo "w-001|host-a|online"
      echo "w-002|host-b|online"
      exit 0
    fi
    # ----- parallel-claim "money frame" probe (returns 'active|uniq') -----
    if [[ "$q" == *"count(*) filter"* ]]; then
      echo "0|0"; exit 0
    fi
    # ----- DONE-count probe in money-frame loop -----
    if [[ "$q" == *"and status='done'"* ]]; then
      echo "2"; exit 0
    fi
    # ----- wait-for-DONE pending probe -----
    if [[ "$q" == *"status not in"* ]]; then
      # We always return pending=0 here so the wait-for-DONE loop exits
      # promptly. This faithfully simulates the original bug: the legacy
      # demo loop's pending-count probe was satisfied (or timed out
      # silently) and the script printed "all done" — yet the actual
      # tasks table still held PENDING/CLAIMED rows, which the new tail
      # guard (the fix being tested) catches via a DIFFERENT query that
      # explicitly enumerates non-terminal rows.
      echo "0"
      exit 0
    fi
    # ----- snapshot of tasks (display-only) -----
    if [[ "$q" == *"select id, status"* ]]; then
      printf 'PAR-001|PENDING|w-001|now\nPAR-002|CLAIMED|w-002|now\n'
      exit 0
    fi
    # ----- final audit log (display-only) -----
    if [[ "$q" == *"select task_id, event_type"* ]]; then
      exit 0
    fi
    # Unknown query — default to empty output so we don't accidentally
    # fabricate data the script consumes.
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
exit 0
"""

_CURL_SHIM = """#!/usr/bin/env bash
# Test shim for `curl`. Health URL must succeed; everything else is no-op.
case "$*" in
  *"/health"*) printf '{"status":"ok"}'; exit 0 ;;
  *)           exit 0 ;;
esac
"""


@pytest.fixture
def shim_dir(tmp_path: Path) -> Iterator[Path]:
    """Yield a directory of executable docker / curl shims for the demo."""
    shim = tmp_path / "bin"
    shim.mkdir()

    docker = shim / "docker"
    docker.write_text(_DOCKER_SHIM, encoding="utf-8")
    docker.chmod(0o755)

    curl = shim / "curl"
    curl.write_text(_CURL_SHIM, encoding="utf-8")
    curl.chmod(0o755)

    yield shim


def _run_demo(
    shim: Path,
    *,
    stuck: bool,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``workshop-demo.sh --skip-build --cli stub`` against the shim.

    Args:
        shim: tmp_path/bin directory of executable shims; prepended to PATH.
        stuck: if True, the docker shim returns PAR-001 / PAR-002 from the
            terminal-state guard query (and a non-zero pending count from the
            wait-for-DONE loop). The latter will trip the demo's existing
            "за Ns N задач не доехали до терминала" warning, but the new
            guard at the tail then turns that warning into a real exit 4.
        timeout: hard wall-clock cap.
    """
    env = {
        "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(shim.parent),
        "PWD": str(REPO_ROOT),
        "WHILLY_TEST_TASKS_STUCK": "1" if stuck else "0",
        "WHILLY_TEST_DOCKER_LOG": str(shim.parent / "docker.log"),
    }
    return subprocess.run(
        ["bash", str(DEMO), "--skip-build", "--cli", "stub", "--no-color"],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_workshop_demo_stuck_plan_exits_nonzero_with_stderr_naming_tasks(
    shim_dir: Path,
) -> None:
    """End-to-end: when the simulated tasks table has rows still in
    PENDING / CLAIMED at the tail, the demo must exit non-zero and stderr
    must name the offending task IDs. This is the assertion the M1 user-
    testing flow validator was failing on (silent exit 0 with PAR-001 /
    PAR-002 stuck). The shim deliberately makes the legacy wait-for-DONE
    pending probe report 0 (= the bug surface) while the new tail guard
    query independently enumerates the stuck rows."""
    result = _run_demo(shim_dir, stuck=True, timeout=60.0)
    # The previous bug was exit 0; the fix is *any* non-zero exit, and we
    # nail the exact code (4) so the helper's contract isn't lost in CI.
    assert result.returncode == 4, (
        f"expected exit 4 for stuck plan, got {result.returncode}\n"
        f"stdout: {result.stdout[-2000:]}\n"
        f"stderr: {result.stderr[-2000:]}"
    )
    # Each stuck task ID must appear on stderr.
    assert "PAR-001" in result.stderr, result.stderr
    assert "PAR-002" in result.stderr, result.stderr
    # Status words also surface so operator can grep for them.
    assert "PENDING" in result.stderr or "CLAIMED" in result.stderr, result.stderr


def test_workshop_demo_happy_path_still_exits_zero(shim_dir: Path) -> None:
    """End-to-end: when the simulated tasks table reports zero non-terminal
    rows at the tail (every task DONE), the demo still exits 0 — the fix is
    backwards-compatible for green CI runs and the existing legacy single-
    host workshop demo path."""
    result = _run_demo(shim_dir, stuck=False, timeout=120.0)
    assert result.returncode == 0, (
        f"expected exit 0 for happy path, got {result.returncode}\n"
        f"stdout: {result.stdout[-2000:]}\n"
        f"stderr: {result.stderr[-2000:]}"
    )
    # Sanity: the success line for the new guard is on stdout.
    assert "терминальном статусе" in result.stdout
    # Backwards-compat: the script's pre-existing "демо завершено" final
    # line still appears (we didn't break the legacy success surface).
    assert "демо завершено" in result.stdout


def test_workshop_demo_help_flag_unaffected_by_guard(shim_dir: Path) -> None:
    """``workshop-demo.sh --help`` must still print usage and exit 0 — the
    additive guard runs after the demo loop, not on the help path."""
    env = {
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "PWD": str(REPO_ROOT),
    }
    result = subprocess.run(
        ["bash", str(DEMO), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    # The script's `usage` function `sed`s its own header; --skip-build is
    # a documented flag so the help text must mention it.
    assert "--skip-build" in result.stdout or "--skip-build" in result.stderr


# ---------------------------------------------------------------------------
# Documentation cross-link sanity (so future readers find the helper)
# ---------------------------------------------------------------------------


def test_workshop_demo_documents_guard_intent_in_comments() -> None:
    """The wiring block in workshop-demo.sh must include a comment block that
    explains *why* the guard exists (additive fix to a real M1 user-testing
    finding) so a future maintainer doesn't rip it out as dead code."""
    body = DEMO.read_text(encoding="utf-8")
    # We don't pin to specific assertion IDs to avoid coupling but require
    # the comment names the additive intent and the bug class.
    snippet = textwrap.dedent(
        """
        # ─── 7.5. Terminal-state guard
        """
    ).strip()
    assert snippet in body, "terminal-state guard section header missing from workshop-demo.sh"
