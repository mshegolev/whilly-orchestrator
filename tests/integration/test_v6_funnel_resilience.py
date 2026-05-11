"""Static-contract gates for the v6-baseline-r3 funnel resilience hardening.

Sibling of ``test_v6_baseline_funnel_paid_plan.py``: pins the wire-level
invariants of feature ``harden-funnel-sidecar-resilience`` so a regression
in the relaxed-keepalive / jittered-backoff / autossh-aware reconnect
loop is caught at PR time rather than at the next 60-min steady-state
VPS run.

Pinned contract:

1. ``scripts/funnel/run.sh`` still passes ``bash -n``.

2. SSH argv exposes the relaxed keepalive triple
   (``ServerAliveInterval=60`` + ``ServerAliveCountMax=5``
   + ``TCPKeepAlive=yes``) and does NOT contain
   ``ExitOnForwardFailure=yes``.

3. The reconnect loop's sleep is jittered at runtime via an
   ``awk 'BEGIN{srand();print int(rand()*...)}'`` snippet (no
   python on the alpine hot path).

4. Each session emits structured stderr lines so operators can
   ``docker logs whilly-cp-funnel | grep 'session ended' | wc -l``
   to count flap events:

   * ``funnel: ssh session up at <iso8601>``
   * ``funnel: ssh session ended after <duration>s, sleeping <jitter>s``
   * ``funnel: ssh session reconnecting (attempt N) at <iso8601>``

5. ``Dockerfile.funnel`` apk-add list contains ``autossh`` so
   the alpine image carries the autossh binary.

6. ``run.sh`` selects ``autossh`` when ``command -v autossh``
   succeeds and falls back to bare ``ssh`` otherwise.

These tests are static / hermetic — they never dial out to a real
``localhost.run`` edge.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "scripts" / "funnel" / "run.sh"
DOCKERFILE_FUNNEL = REPO_ROOT / "Dockerfile.funnel"


@pytest.fixture(scope="module")
def run_sh_text() -> str:
    return RUN_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE_FUNNEL.read_text(encoding="utf-8")


def _have_bash() -> bool:
    return shutil.which("bash") is not None


def _dump_ssh_args(env: dict[str, str]) -> list[str]:
    if not _have_bash():
        pytest.skip("bash not available on this runner")
    base_env = {
        "FUNNEL_DUMP_SSH_ARGS": "1",
        "FUNNEL_SKIP_KEY_CHECK": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
    }
    base_env.update(env)
    proc = subprocess.run(
        ["bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        env=base_env,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"funnel run.sh dump-mode exited non-zero: rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return [line for line in proc.stdout.splitlines() if line]


# ── 1. Files still parse ──────────────────────────────────────────────────


def test_run_sh_bash_syntax_after_hardening() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, "-n", str(RUN_SH)], capture_output=True, text=True)
    assert res.returncode == 0, f"run.sh syntax error after hardening:\n{res.stderr}"


# ── 2. Relaxed keepalive triple in SSH argv ───────────────────────────────


def test_server_alive_interval_60() -> None:
    args = _dump_ssh_args({})
    assert "ServerAliveInterval=60" in args, f"v6-baseline-r3 hardening relaxes ServerAliveInterval to 60s: {args}"


def test_server_alive_count_max_5() -> None:
    args = _dump_ssh_args({})
    assert "ServerAliveCountMax=5" in args, f"v6-baseline-r3 hardening relaxes ServerAliveCountMax to 5: {args}"


def test_tcp_keepalive_yes_present() -> None:
    args = _dump_ssh_args({})
    assert "TCPKeepAlive=yes" in args, (
        f"TCPKeepAlive=yes must be present so the OS-layer keepalive is also enabled: {args}"
    )


def test_exit_on_forward_failure_removed() -> None:
    args = _dump_ssh_args({})
    forbidden = [a for a in args if a.startswith("ExitOnForwardFailure")]
    assert forbidden == [], (
        "ExitOnForwardFailure=yes must be REMOVED — the supervisor loop / autossh "
        f"handles reconnects on missed keepalives: {args}"
    )


def test_keepalive_overrides_propagate() -> None:
    args = _dump_ssh_args(
        {
            "FUNNEL_SERVER_ALIVE_INTERVAL": "120",
            "FUNNEL_SERVER_ALIVE_COUNT_MAX": "10",
            "FUNNEL_TCP_KEEPALIVE": "no",
        }
    )
    assert "ServerAliveInterval=120" in args, args
    assert "ServerAliveCountMax=10" in args, args
    assert "TCPKeepAlive=no" in args, args


# ── 3. Jittered backoff sleep ─────────────────────────────────────────────


def test_jitter_uses_awk_srand_rand(run_sh_text: str) -> None:
    pattern = re.compile(r"awk[^\n]*BEGIN\s*\{\s*srand\(\)\s*;\s*print\s+int\(rand\(\)\s*\*")
    assert pattern.search(run_sh_text), (
        "run.sh must compute jitter with awk 'BEGIN{srand();print int(rand()*...)}' (no python on hot path)"
    )


def test_jitter_max_env_referenced(run_sh_text: str) -> None:
    assert "FUNNEL_RETRY_BACKOFF_JITTER_MAX" in run_sh_text, (
        "run.sh must reference FUNNEL_RETRY_BACKOFF_JITTER_MAX so operators can tune the jitter window"
    )


def test_sleep_value_is_computed_not_literal(run_sh_text: str) -> None:
    fixed_literal = re.compile(r'sleep\s+"\$FUNNEL_RETRY_BACKOFF_SECONDS"')
    assert not fixed_literal.search(run_sh_text), (
        "run.sh must NOT sleep on a fixed FUNNEL_RETRY_BACKOFF_SECONDS — jitter must be added"
    )
    computed = re.compile(r'sleep\s+"?\$\{?sleep_total')
    assert computed.search(run_sh_text), "run.sh must sleep on a computed total (FUNNEL_RETRY_BACKOFF_SECONDS + jitter)"


def test_compute_jitter_function_runs() -> None:
    if not _have_bash():
        pytest.skip("bash not available on this runner")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"set -eo pipefail; source {RUN_SH!s}; FUNNEL_RETRY_BACKOFF_JITTER_MAX=10 compute_jitter_seconds",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, f"compute_jitter_seconds failed: {proc.stderr!r}"
    raw = proc.stdout.strip()
    assert raw.isdigit(), f"compute_jitter_seconds must print an integer, got {raw!r}"
    value = int(raw)
    assert 0 <= value <= 10, f"jitter value {value} out of range [0, 10]"


# ── 4. Structured-log invariants ──────────────────────────────────────────


@pytest.mark.parametrize(
    "needle",
    [
        "ssh session up at",
        "ssh session ended after",
        "ssh session reconnecting (attempt",
    ],
)
def test_structured_log_phrase_present(run_sh_text: str, needle: str) -> None:
    assert needle in run_sh_text, f"run.sh must emit structured-log line containing {needle!r} for operator grep flow"


def test_session_ended_log_includes_duration_and_sleep(run_sh_text: str) -> None:
    pattern = re.compile(r'"ended after \$\{?duration\}?s, sleeping \$\{?sleep_total\}?s"')
    assert pattern.search(run_sh_text), (
        "session-ended log must report both duration and sleep_total: "
        "'ssh session ended after <duration>s, sleeping <sleep_total>s'"
    )


def test_session_logs_go_to_stderr(run_sh_text: str) -> None:
    pattern = re.compile(r"emit_session_event\s*\([^)]*\)\s*\{[^}]*>&2", re.DOTALL)
    assert pattern.search(run_sh_text), (
        "emit_session_event helper must redirect to stderr (>&2) so operator greps capture it via "
        "`docker logs whilly-cp-funnel`"
    )


def test_iso8601_timestamp_helper_present(run_sh_text: str) -> None:
    assert re.search(r"iso8601_now\s*\(\)\s*\{", run_sh_text), (
        "run.sh must define an iso8601_now helper used by the session-up / reconnect log lines"
    )
    assert '"up at $(iso8601_now)"' in run_sh_text, (
        "session-up log must call iso8601_now() so the timestamp is RFC3339-compatible"
    )
    assert '"reconnecting (attempt ${attempt}) at $(iso8601_now)"' in run_sh_text, (
        "session-reconnecting log must include iso8601_now() timestamp + attempt counter"
    )


# ── 5. Dockerfile.funnel apk-add includes autossh ─────────────────────────


def test_dockerfile_funnel_includes_autossh(dockerfile_text: str) -> None:
    apk_block = re.search(r"apk add\s+--no-cache\s+([^&]+)&&", dockerfile_text, re.DOTALL)
    assert apk_block is not None, "Dockerfile.funnel must keep the `apk add --no-cache ... &&` block"
    packages = re.findall(r"[A-Za-z0-9_+-]+", apk_block.group(1))
    assert "autossh" in packages, (
        f"Dockerfile.funnel apk add must include `autossh` for v6-baseline-r3 hardening: {packages}"
    )


# ── 6. autossh-aware transport selection in run.sh ────────────────────────


def test_run_sh_selects_autossh_when_present(run_sh_text: str) -> None:
    assert re.search(r"command\s+-v\s+autossh", run_sh_text), (
        "run.sh must probe for autossh on PATH via `command -v autossh`"
    )
    assert re.search(r"autossh\s+-M\s+0\b", run_sh_text), (
        "run.sh must invoke `autossh -M 0` (autossh's own monitor port disabled; rely on SSH keepalives)"
    )


def test_run_sh_falls_back_to_bare_ssh(run_sh_text: str) -> None:
    pattern = re.compile(
        r'if\s+\[\s+"\$transport"\s+=\s+"autossh"\s+\];\s*then.*?else\s+ssh\s+"\$\{ssh_args\[@\]\}"',
        re.DOTALL,
    )
    assert pattern.search(run_sh_text), "run.sh must keep the bare-ssh fallback branch when autossh is not on PATH"


def test_run_sh_supervisor_loop_still_present(run_sh_text: str) -> None:
    assert re.search(r"while\s+true\s*;\s*do", run_sh_text), (
        "run.sh must keep the supervisor `while true` loop as defence-in-depth around autossh / ssh"
    )
