"""Integration test for the M3 funnel stable-URL SSH-key + custom-domain wiring.

End-to-end smoke for ``m3-funnel-stable-url-via-ssh-key``:

* Invokes ``scripts/funnel/run.sh`` with ``FUNNEL_DUMP_SSH_ARGS=1`` so the
  script prints the constructed ``ssh`` argv (one token per line) and
  exits without dialling out. This pins the *contract* between the
  M2 anonymous-rotating tier and the new M3 SSH-key-stable / custom-domain
  tiers without requiring a live ``localhost.run`` connection.

* Asserts:
    1. Default invocation (no ``FUNNEL_SSH_KEY_PATH``) preserves the v4.5
       byte-equivalent ``nokey@localhost.run`` flow with no ``-i``.
    2. ``FUNNEL_SSH_KEY_PATH=/keys/funnel_id`` switches to ``-i`` +
       ``localhost.run@localhost.run`` (anonymous nokey username
       replaced).
    3. ``FUNNEL_CUSTOM_DOMAIN=tunnel.example.com`` (paid tier) prepends
       the domain to the ``-R`` forward spec.
    4. Operator-overridden ``FUNNEL_REMOTE_USER`` is respected when
       paired with an SSH key (we don't unilaterally clobber it).
    5. ``IdentitiesOnly=yes`` is set whenever ``FUNNEL_SSH_KEY_PATH`` is
       set so the sidecar never falls back to a host-side ssh-agent
       holding an unrelated key.
    6. The compose override ``docker-compose.funnel-stable.yml`` lints
       green when stacked on top of either base file.

The test does NOT exercise the publish path — that is already covered
by ``test_funnel_sidecar_url_publish.py``. The two tests together
pin the publish contract (M2) and the auth-tier contract (M3).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
RUN_SH: Path = REPO_ROOT / "scripts" / "funnel" / "run.sh"
COMPOSE_DEMO: Path = REPO_ROOT / "docker-compose.demo.yml"
COMPOSE_CONTROL_PLANE: Path = REPO_ROOT / "docker-compose.control-plane.yml"
COMPOSE_OVERRIDE: Path = REPO_ROOT / "docker-compose.funnel-stable.yml"


def _have_bash() -> bool:
    return shutil.which("bash") is not None


def _have_docker_compose() -> bool:
    """Return True if a usable docker-compose CLI is on PATH."""
    if shutil.which("docker-compose") is not None:
        return True
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=10)
    return proc.returncode == 0


def _dump_ssh_args(env: dict[str, str]) -> list[str]:
    """Run scripts/funnel/run.sh in dump-mode, return the printed ssh tokens."""
    if not _have_bash():
        pytest.skip("bash not available on this runner")
    cmd = ["bash", str(RUN_SH)]
    base_env = {"FUNNEL_DUMP_SSH_ARGS": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    base_env.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=base_env, timeout=15)
    assert proc.returncode == 0, (
        f"funnel run.sh dump-mode exited non-zero: rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return [line for line in proc.stdout.splitlines() if line]


def test_run_sh_exists_and_is_executable() -> None:
    assert RUN_SH.is_file(), f"missing funnel sidecar entrypoint at {RUN_SH}"
    assert RUN_SH.stat().st_mode & 0o111, f"{RUN_SH} is not executable"


def test_run_sh_references_funnel_ssh_key_path_env_var() -> None:
    """The script literally references FUNNEL_SSH_KEY_PATH (verification step #1)."""
    text = RUN_SH.read_text()
    assert "FUNNEL_SSH_KEY_PATH" in text, "FUNNEL_SSH_KEY_PATH must be wired into run.sh"
    assert "FUNNEL_CUSTOM_DOMAIN" in text, "FUNNEL_CUSTOM_DOMAIN must be wired into run.sh"


def test_default_anonymous_tier_is_byte_equivalent_to_v45() -> None:
    """No SSH key set → preserves the M2 anon-rotating ssh argv."""
    args = _dump_ssh_args({})
    assert "-i" not in args, f"unexpected `-i` in default argv: {args}"
    assert "IdentitiesOnly=yes" not in args, f"unexpected IdentitiesOnly in default argv: {args}"
    assert "nokey@localhost.run" in args, f"expected nokey@localhost.run target: {args}"
    assert "80:control-plane:8000" in args, f"expected default forward spec: {args}"


def test_ssh_key_path_switches_to_account_bound_user() -> None:
    """FUNNEL_SSH_KEY_PATH set → -i + localhost.run@localhost.run."""
    args = _dump_ssh_args({"FUNNEL_SSH_KEY_PATH": "/keys/funnel_id"})
    assert "-i" in args, f"expected -i flag when FUNNEL_SSH_KEY_PATH is set: {args}"
    i_idx = args.index("-i")
    assert args[i_idx + 1] == "/keys/funnel_id", f"-i value mismatch: {args[i_idx + 1]!r}"
    assert "IdentitiesOnly=yes" in args, (
        f"IdentitiesOnly=yes must be set with SSH key to avoid host ssh-agent leak: {args}"
    )
    assert "localhost.run@localhost.run" in args, f"nokey username must flip to localhost.run when key is set: {args}"
    assert "nokey@localhost.run" not in args, f"nokey username must NOT remain when key is set: {args}"


def test_custom_domain_prepends_to_forward_spec() -> None:
    """FUNNEL_CUSTOM_DOMAIN set → `-R <domain>:80:<host>:<port>`."""
    args = _dump_ssh_args(
        {
            "FUNNEL_SSH_KEY_PATH": "/keys/funnel_id",
            "FUNNEL_CUSTOM_DOMAIN": "tunnel.example.com",
        }
    )
    assert "-R" in args, f"expected -R flag in argv: {args}"
    r_idx = args.index("-R")
    forward = args[r_idx + 1]
    assert forward == "tunnel.example.com:80:control-plane:8000", (
        f"expected custom domain prepended to forward spec; got {forward!r}"
    )


def test_custom_domain_without_ssh_key_still_constructs_argv() -> None:
    """FUNNEL_CUSTOM_DOMAIN alone (misconfiguration but accepted) still builds argv.

    Operators are most likely to set both, but if only the domain is set
    we don't crash — the connect attempt will simply fail at the
    localhost.run edge with "remote forwarding failed" and the sidecar's
    ExitOnForwardFailure=yes will surface it in `docker logs funnel`.
    """
    args = _dump_ssh_args({"FUNNEL_CUSTOM_DOMAIN": "tunnel.example.com"})
    r_idx = args.index("-R")
    forward = args[r_idx + 1]
    assert forward == "tunnel.example.com:80:control-plane:8000"
    assert "nokey@localhost.run" in args, "user must remain nokey when no SSH key is set"


def test_explicit_remote_user_is_respected_with_ssh_key() -> None:
    """Operators who set FUNNEL_REMOTE_USER explicitly are NOT clobbered."""
    args = _dump_ssh_args(
        {
            "FUNNEL_SSH_KEY_PATH": "/keys/funnel_id",
            "FUNNEL_REMOTE_USER": "alice",
        }
    )
    assert "alice@localhost.run" in args, f"explicit FUNNEL_REMOTE_USER must be honoured: {args}"
    assert "localhost.run@localhost.run" not in args, (
        f"explicit user must NOT be replaced by the localhost.run default: {args}"
    )


def test_local_host_and_port_overrides_propagate() -> None:
    """FUNNEL_LOCAL_HOST / FUNNEL_LOCAL_PORT propagate into the -R spec."""
    args = _dump_ssh_args(
        {
            "FUNNEL_SSH_KEY_PATH": "/keys/funnel_id",
            "FUNNEL_LOCAL_HOST": "127.0.0.1",
            "FUNNEL_LOCAL_PORT": "8123",
            "FUNNEL_CUSTOM_DOMAIN": "foo.example.com",
        }
    )
    r_idx = args.index("-R")
    assert args[r_idx + 1] == "foo.example.com:80:127.0.0.1:8123"


def test_compose_override_lints_with_demo_base_file() -> None:
    """`docker-compose.funnel-stable.yml` is valid when stacked on demo.yml."""
    if not _have_docker_compose():
        pytest.skip("docker-compose CLI not available")
    if not COMPOSE_OVERRIDE.is_file():
        pytest.skip(f"override file missing at {COMPOSE_OVERRIDE}")
    proc = subprocess.run(
        [
            "docker-compose",
            "-f",
            str(COMPOSE_DEMO),
            "-f",
            str(COMPOSE_OVERRIDE),
            "config",
            "-q",
        ],
        capture_output=True,
        text=True,
        env={
            **{k: v for k, v in __import__("os").environ.items() if k not in {"FUNNEL_SSH_KEY_HOST_PATH"}},
            "FUNNEL_SSH_KEY_HOST_PATH": "/tmp/whilly-funnel-fakekey",
        },
        timeout=30,
    )
    assert proc.returncode == 0, f"docker-compose config failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"


def test_compose_override_lints_with_control_plane_base_file() -> None:
    """`docker-compose.funnel-stable.yml` is valid when stacked on control-plane.yml."""
    if not _have_docker_compose():
        pytest.skip("docker-compose CLI not available")
    if not COMPOSE_OVERRIDE.is_file():
        pytest.skip(f"override file missing at {COMPOSE_OVERRIDE}")
    proc = subprocess.run(
        [
            "docker-compose",
            "-f",
            str(COMPOSE_CONTROL_PLANE),
            "-f",
            str(COMPOSE_OVERRIDE),
            "config",
            "-q",
        ],
        capture_output=True,
        text=True,
        env={
            **{k: v for k, v in __import__("os").environ.items() if k not in {"FUNNEL_SSH_KEY_HOST_PATH"}},
            "FUNNEL_SSH_KEY_HOST_PATH": "/tmp/whilly-funnel-fakekey",
        },
        timeout=30,
    )
    assert proc.returncode == 0, f"docker-compose config failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"


def test_compose_override_fails_fast_when_host_key_path_unset() -> None:
    """Without FUNNEL_SSH_KEY_HOST_PATH set, compose must abort with a clear error.

    The fail-fast guard in the override file (`${VAR:?...}` syntax) is
    deliberate: the bind-mount is only meaningful with a real key path.
    Letting compose silently accept an empty host-side path would create
    a subtle runtime failure (sidecar would crash with "no such file
    /keys/funnel_id" inside the container) which is much harder to
    diagnose than the fail-fast at compose-time.
    """
    if not _have_docker_compose():
        pytest.skip("docker-compose CLI not available")
    if not COMPOSE_OVERRIDE.is_file():
        pytest.skip(f"override file missing at {COMPOSE_OVERRIDE}")
    import os as _os

    sanitized_env = {k: v for k, v in _os.environ.items() if k != "FUNNEL_SSH_KEY_HOST_PATH"}
    proc = subprocess.run(
        [
            "docker-compose",
            "-f",
            str(COMPOSE_CONTROL_PLANE),
            "-f",
            str(COMPOSE_OVERRIDE),
            "config",
            "-q",
        ],
        capture_output=True,
        text=True,
        env=sanitized_env,
        timeout=30,
    )
    assert proc.returncode != 0, (
        f"docker-compose config should fail without FUNNEL_SSH_KEY_HOST_PATH; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "funnel_ssh_key_host_path" in combined, f"error must name the missing variable; got {combined!r}"


def test_distributed_setup_doc_covers_three_modes() -> None:
    """docs/Distributed-Setup.md must describe all three exposure modes.

    Mirrors the M3 expectedBehavior contract: documentation covers
    anonymous-rotating, SSH-key-stable, and custom-domain modes.
    """
    doc = REPO_ROOT / "docs" / "Distributed-Setup.md"
    assert doc.is_file(), f"missing doc at {doc}"
    text = doc.read_text()
    assert "Stable URL via SSH key" in text, "doc must cover the SSH-key-stable mode"
    assert "Custom domain" in text, "doc must cover the paid-tier custom-domain mode"
    assert "FUNNEL_SSH_KEY_PATH" in text, "doc must reference the FUNNEL_SSH_KEY_PATH env var"
    assert "FUNNEL_CUSTOM_DOMAIN" in text, "doc must reference the FUNNEL_CUSTOM_DOMAIN env var"
    assert "WHILLY_FUNNEL_URL_SOURCE=static" in text, (
        "doc must explain the worker-side trade-off (static URL when stable)"
    )
    assert "https://admin.localhost.run/" in text, "doc must point operators at the localhost.run admin console"
