"""Static-contract gates for the v6.0 paid-plan funnel sidecar.

Mirrors the pattern of `test_v6_baseline_vps_scripts.py`: pins the
shipped artefacts of feature `replace-funnel-with-lhr-paid` so a
regression in the paid-plan funnel wiring is caught at PR time
rather than at the next live VPS bringup. Does NOT require SSH
access to the VPS — the live multi-host smoke is the operator's
job and is intentionally not a hermetic CI step.

Pinned contract:

1. ``Dockerfile.funnel`` and ``scripts/funnel/run.sh`` exist, are
   executable (run.sh), and pass ``bash -n`` (run.sh) /
   ``docker buildx build`` lint (Dockerfile, syntax only).

2. The funnel run.sh:

   * references the new ``LHR_HOSTNAME``, ``LHR_REMOTE_USER``,
     ``LHR_REMOTE_HOST``, ``LHR_LOCAL_TARGET`` env vars;
   * builds an SSH argv that pins the reverse-tunnel hostname to
     ``${LHR_HOSTNAME}`` (default ``whilly-orchestrator.lhr.rocks``)
     with the forward spec
     ``${LHR_HOSTNAME}:80:control-plane:8000``;
   * passes the SSH key with ``IdentitiesOnly=yes -i
     /etc/whilly-funnel/ssh-key`` (in-container path);
   * dials ``plan@localhost.run`` (the paid-plan account user);
   * sets keepalives ``ServerAliveInterval=60``,
     ``ServerAliveCountMax=5``, and ``TCPKeepAlive=yes`` (relaxed
     in v6-baseline-r3 hardening — see
     ``test_v6_funnel_resilience.py``); ``ExitOnForwardFailure=yes``
     is intentionally absent — the supervisor loop / autossh
     handles reconnects;
   * sets ``StrictHostKeyChecking=accept-new`` for first-run TOFU;
   * fails fast with a clear stderr referencing
     ``https://localhost.run/dashboard/ssh-keys/`` when the SSH key
     file is missing on the VPS at the configured path;
   * carries no ``lhr.life`` literals (regex assertion for migration
     completion).

3. ``docker-compose.control-plane.yml`` ``funnel`` service:

   * declares ``LHR_HOSTNAME``, ``LHR_REMOTE_USER``, ``LHR_REMOTE_HOST``,
     ``LHR_LOCAL_TARGET`` env entries with the documented defaults;
   * mounts the host SSH key path (``${LHR_SSH_KEY_PATH:?must-be-set}``)
     into the container at ``/etc/whilly-funnel/ssh-key`` read-only;
   * lints green via ``docker-compose config -q``.

4. ``scripts/v6-baseline-vps-up.sh`` resolves the public URL via
   ``LHR_HOSTNAME`` (single curl probe — no rotation retry budget).

5. ``scripts/v6-baseline-vps-doctor.sh`` reads the env-pinned
   hostname, runs a 3-probe stability window within 15 seconds,
   and ``--help`` still works.

6. The Tailscale-removed invariant still holds (no ``\\btailscale``
   match anywhere in the funnel surface).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_FUNNEL = REPO_ROOT / "Dockerfile.funnel"
RUN_SH = REPO_ROOT / "scripts" / "funnel" / "run.sh"
COMPOSE_CONTROL_PLANE = REPO_ROOT / "docker-compose.control-plane.yml"
UP_SCRIPT = REPO_ROOT / "scripts" / "v6-baseline-vps-up.sh"
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "v6-baseline-vps-doctor.sh"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE_FUNNEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run_sh_text() -> str:
    return RUN_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_CONTROL_PLANE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def up_text() -> str:
    return UP_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doctor_text() -> str:
    return DOCTOR_SCRIPT.read_text(encoding="utf-8")


def _have_bash() -> bool:
    return shutil.which("bash") is not None


def _have_docker_compose() -> bool:
    if shutil.which("docker-compose") is not None:
        return True
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=10)
    return proc.returncode == 0


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


# ── 1. Files exist + bash syntax + executable ─────────────────────────────


def test_dockerfile_funnel_exists() -> None:
    assert DOCKERFILE_FUNNEL.is_file(), f"missing {DOCKERFILE_FUNNEL}"


def test_run_sh_exists_and_executable() -> None:
    assert RUN_SH.is_file(), f"missing {RUN_SH}"
    assert os.access(RUN_SH, os.X_OK), f"{RUN_SH} is not executable"


def test_run_sh_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, "-n", str(RUN_SH)], capture_output=True, text=True)
    assert res.returncode == 0, f"run.sh syntax error:\n{res.stderr}"


# ── 2. Env wiring in run.sh ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "var",
    [
        "LHR_HOSTNAME",
        "LHR_REMOTE_USER",
        "LHR_REMOTE_HOST",
        "LHR_LOCAL_TARGET",
    ],
)
def test_run_sh_references_lhr_env_vars(run_sh_text: str, var: str) -> None:
    assert var in run_sh_text, f"run.sh must reference ${var}"


def test_run_sh_default_lhr_hostname(run_sh_text: str) -> None:
    assert "LHR_HOSTNAME:-whilly-orchestrator.lhr.rocks" in run_sh_text


def test_run_sh_default_lhr_remote_user(run_sh_text: str) -> None:
    assert "LHR_REMOTE_USER:-plan@localhost.run" in run_sh_text


def test_run_sh_default_local_target(run_sh_text: str) -> None:
    assert "LHR_LOCAL_TARGET:-control-plane:8000" in run_sh_text


def test_run_sh_dashboard_url_in_error_message(run_sh_text: str) -> None:
    assert "https://localhost.run/dashboard/ssh-keys/" in run_sh_text, (
        "run.sh must print the dashboard URL in stderr when the SSH key is missing or auth fails"
    )


def test_run_sh_no_lhr_life_literals(run_sh_text: str) -> None:
    forbidden = re.compile(r"lhr\.life", re.IGNORECASE)
    assert not forbidden.search(run_sh_text), (
        "run.sh must not contain any 'lhr.life' literals after paid-plan migration"
    )


def test_run_sh_no_nokey_user(run_sh_text: str) -> None:
    forbidden = re.compile(r"\bnokey\b")
    assert not forbidden.search(run_sh_text), "run.sh must not reference the v5.0 free-tier 'nokey' user"


def test_run_sh_no_tailscale_references(run_sh_text: str) -> None:
    forbidden = re.compile(r"\btailscale", re.IGNORECASE)
    assert not forbidden.search(run_sh_text)


# ── 3. SSH argv shape (paid plan) ─────────────────────────────────────────


def test_default_ssh_argv_targets_paid_plan() -> None:
    args = _dump_ssh_args({})
    assert "plan@localhost.run" in args, f"default SSH target must be plan@localhost.run: {args}"
    assert "-i" in args, f"SSH key must be wired with -i: {args}"
    i_idx = args.index("-i")
    assert args[i_idx + 1] == "/etc/whilly-funnel/ssh-key"
    assert "IdentitiesOnly=yes" in args
    assert "-N" in args, "the paid-plan SSH session does not need a remote shell — -N must be set"


def test_default_ssh_argv_pins_hostname() -> None:
    args = _dump_ssh_args({})
    assert "-R" in args
    r_idx = args.index("-R")
    assert args[r_idx + 1] == "whilly-orchestrator.lhr.rocks:80:control-plane:8000"


def test_keepalives_set() -> None:
    args = _dump_ssh_args({})
    assert "ServerAliveInterval=60" in args, f"ServerAliveInterval must be 60s: {args}"
    assert "ServerAliveCountMax=5" in args, f"ServerAliveCountMax must be 5: {args}"
    assert "TCPKeepAlive=yes" in args, f"TCPKeepAlive must be yes: {args}"
    assert "ExitOnForwardFailure=yes" not in args, (
        "ExitOnForwardFailure=yes must be REMOVED — supervisor loop / autossh handles reconnects "
        f"(harden-funnel-sidecar-resilience): {args}"
    )


def test_strict_host_key_checking_accept_new() -> None:
    args = _dump_ssh_args({})
    assert "StrictHostKeyChecking=accept-new" in args


def test_lhr_hostname_override_propagates() -> None:
    args = _dump_ssh_args({"LHR_HOSTNAME": "alt.example.com"})
    r_idx = args.index("-R")
    assert args[r_idx + 1] == "alt.example.com:80:control-plane:8000"


def test_lhr_local_target_override_propagates() -> None:
    args = _dump_ssh_args({"LHR_LOCAL_TARGET": "127.0.0.1:9099"})
    r_idx = args.index("-R")
    assert args[r_idx + 1] == "whilly-orchestrator.lhr.rocks:80:127.0.0.1:9099"


def test_lhr_remote_user_override_propagates() -> None:
    args = _dump_ssh_args({"LHR_REMOTE_USER": "alice@localhost.run"})
    assert "alice@localhost.run" in args


# ── 4. Missing key file → fail-fast with clear stderr ─────────────────────


def test_missing_ssh_key_exits_nonzero_with_dashboard_hint(tmp_path: Path) -> None:
    if not _have_bash():
        pytest.skip("bash not available on this runner")
    fake_key_path = tmp_path / "no-such-key"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "LHR_SSH_KEY_PATH_INSIDE": str(fake_key_path),
        "FUNNEL_ONESHOT": "1",
    }
    proc = subprocess.run(
        ["bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode != 0, (
        f"funnel must exit non-zero when the SSH key is missing\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    combined_stderr = proc.stderr.lower()
    assert "ssh key not found" in combined_stderr, f"stderr must explain the missing key: {proc.stderr!r}"
    assert "https://localhost.run/dashboard/ssh-keys/" in proc.stderr, (
        f"stderr must cite the dashboard URL: {proc.stderr!r}"
    )


# ── 5. docker-compose.control-plane.yml — env + volume mount ──────────────


@pytest.mark.parametrize(
    "needle",
    [
        "LHR_HOSTNAME",
        "LHR_REMOTE_USER",
        "LHR_REMOTE_HOST",
        "LHR_LOCAL_TARGET",
        "/etc/whilly-funnel/ssh-key",
        "${LHR_SSH_KEY_PATH:?",
    ],
)
def test_compose_funnel_service_wires_paid_plan(compose_text: str, needle: str) -> None:
    assert needle in compose_text, f"docker-compose.control-plane.yml must mention {needle!r} for the paid-plan funnel"


def test_compose_funnel_no_legacy_funnel_remote_user(compose_text: str) -> None:
    assert "FUNNEL_REMOTE_USER" not in compose_text, (
        "docker-compose.control-plane.yml must not reference the v5.0 FUNNEL_REMOTE_USER (replaced by LHR_REMOTE_USER)"
    )


def test_compose_funnel_no_lhr_life_literal(compose_text: str) -> None:
    forbidden = re.compile(r"lhr\.life", re.IGNORECASE)
    assert not forbidden.search(compose_text)


def test_compose_control_plane_lints_green() -> None:
    if not _have_docker_compose():
        pytest.skip("docker-compose CLI not available")
    env = {
        **{k: v for k, v in os.environ.items() if k != "LHR_SSH_KEY_PATH"},
        "LHR_SSH_KEY_PATH": "/tmp/whilly-fake-key",
    }
    proc = subprocess.run(
        ["docker-compose", "-f", str(COMPOSE_CONTROL_PLANE), "config", "-q"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"compose lint failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"


# ── 6. v6-baseline-vps-up.sh / -doctor.sh wired to LHR_HOSTNAME ──────────


def test_up_script_resolves_url_from_lhr_hostname(up_text: str) -> None:
    assert "LHR_HOSTNAME:-whilly-orchestrator.lhr.rocks" in up_text
    assert "https://${LHR_HOSTNAME}" in up_text or 'https://${LHR_HOSTNAME}"' in up_text


def test_up_script_no_rotation_retry_budget(up_text: str) -> None:
    assert "no rotation retry budget" in up_text or "single curl" in up_text


def test_up_script_no_lhr_life_literals(up_text: str) -> None:
    forbidden = re.compile(r"lhr\.life", re.IGNORECASE)
    assert not forbidden.search(up_text)


def test_doctor_script_resolves_url_from_lhr_hostname(doctor_text: str) -> None:
    assert "LHR_HOSTNAME:-whilly-orchestrator.lhr.rocks" in doctor_text


def test_doctor_script_uses_3_probe_stability_window(doctor_text: str) -> None:
    assert "HEALTH_PROBE_COUNT=3" in doctor_text
    assert "HEALTH_PROBE_WINDOW_SECONDS=15" in doctor_text


def test_doctor_script_no_lhr_life_literals_in_logic(doctor_text: str) -> None:
    forbidden = re.compile(r"lhr\.life", re.IGNORECASE)
    assert not forbidden.search(doctor_text)


def test_doctor_help_flag_still_works() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run([bash, str(DOCTOR_SCRIPT), "--help"], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"doctor --help exited {proc.returncode}: {proc.stderr!r}"
    out = proc.stdout + proc.stderr
    assert "vps doctor" in out.lower() or "doctor" in out.lower()


# ── 7. Dockerfile.funnel mounts the in-container key path ────────────────


def test_dockerfile_funnel_creates_key_mountpoint(dockerfile_text: str) -> None:
    assert "/etc/whilly-funnel" in dockerfile_text, (
        "Dockerfile.funnel must create /etc/whilly-funnel as the SSH key mount target"
    )


def test_dockerfile_funnel_no_lhr_life_literals(dockerfile_text: str) -> None:
    forbidden = re.compile(r"lhr\.life", re.IGNORECASE)
    assert not forbidden.search(dockerfile_text), (
        "Dockerfile.funnel must not contain any 'lhr.life' literals after paid-plan migration"
    )


def test_dockerfile_funnel_no_nokey_references(dockerfile_text: str) -> None:
    forbidden = re.compile(r"\bnokey\b")
    assert not forbidden.search(dockerfile_text)
