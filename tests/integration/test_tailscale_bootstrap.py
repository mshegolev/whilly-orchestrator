"""Tailscale worker bootstrap integration test (v4.4.x — m1-tailscale-worker-bootstrap).

Backs the ``m1-tailscale-worker-bootstrap`` feature: the worker container
optionally joins a private Tailscale tailnet at startup so it can reach
a control-plane that lives on the operator's laptop tailnet without any
public-internet exposure (no ngrok, no Funnel, no public IP).

This test exercises THREE layers:

1. **Static check** — ``docker/entrypoint.sh`` includes the
   ``TAILSCALE_AUTHKEY`` guard, the ``--tun=userspace-networking`` flag,
   and the ``--advertise-tags=tag:whilly-worker`` advertisement. These
   are the contractual hooks the feature is required to ship; static
   parsing catches accidental regressions without needing Docker.

2. **Default-build path (``WHILLY_INCLUDE_TAILSCALE=1``)** — build the
   ``worker`` stage and assert ``tailscale --version`` returns 0 inside.
   Confirms the static-binary install ships the binary on PATH for the
   default build.

3. **Slim-build path (``WHILLY_INCLUDE_TAILSCALE=0``)** — build the
   ``worker`` stage without tailscale and assert the binary is NOT on
   PATH. Confirms the build-arg actually gates the install (so an
   operator can opt out).

Skipping policy
---------------
* Static check (1) runs anywhere; no Docker required.
* Build-based checks (2)+(3) require a reachable Docker daemon.
  ``pytest.skip``s with a clear reason when ``docker info`` fails — never
  *fails* due to environment unavailability.

Real-tailnet validation (running tailscale up against a live tailnet
with a real auth key) is e2e validation territory and explicitly OUT of
scope here per the feature spec.

Sibling
-------
* ``tests/integration/test_dockerfile_agent_clis_arg.py`` covers the
  npm-side ``WHILLY_AGENT_CLIS`` build-arg invariant.
* ``tests/integration/test_worker_image_import_purity.py`` covers the
  Python-side dep-closure invariant.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Repo root = this file's grandparent's grandparent (tests/integration → tests → repo)
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

ENTRYPOINT_SH: Path = REPO_ROOT / "docker" / "entrypoint.sh"
DOCKERFILE: Path = REPO_ROOT / "Dockerfile"

# Tags for the two builds under test. Held distinct so a failed test in
# either path doesn't shadow the other when the same dev iterates locally.
DEFAULT_TAG: str = "whilly-worker:tailscale-default-test"
SLIM_TAG: str = "whilly-worker:tailscale-slim-test"

# Build timeout — first run on a clean machine pulls python:3.12-slim and
# installs node + npm. Generous ceiling; warm BuildKit cache finishes fast.
BUILD_TIMEOUT_SECONDS: float = 1200.0

# Run timeout — `tailscale --version` / `which tailscale` are single
# operations. 60s ceiling is generous.
RUN_TIMEOUT_SECONDS: float = 60.0


# ─── Docker availability gate (mirrors test_dockerfile_agent_clis_arg.py) ───


def _docker_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the Docker daemon."""
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        proc = subprocess.run(  # noqa: S603 — fully literal argv
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"`docker info` did not return: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        first_line = proc.stderr.decode("utf-8", "replace").splitlines()[0:1]
        hint = first_line[0] if first_line else "no stderr captured"
        return False, f"`docker info` exited {proc.returncode}: {hint}"
    return True, "ok"


_DOCKER_OK, _DOCKER_REASON = _docker_available()
DOCKER_REQUIRED = pytest.mark.skipif(not _DOCKER_OK, reason=_DOCKER_REASON)


def _run(
    cmd: list[str],
    *,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return ``CompletedProcess`` with text I/O."""
    return subprocess.run(  # noqa: S603 — argv assembled from literals
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _docker_rmi(tag: str) -> None:
    """Best-effort image cleanup. Errors are swallowed."""
    subprocess.run(  # noqa: S603 — literal argv
        ["docker", "rmi", "-f", tag],
        capture_output=True,
        timeout=30,
        check=False,
    )


# ─── (1) Static checks: entrypoint.sh + Dockerfile ──────────────────────────


def test_entrypoint_has_tailscale_authkey_guard() -> None:
    """``docker/entrypoint.sh`` includes the ``TAILSCALE_AUTHKEY`` opt-in guard.

    The contract is:
      * The bootstrap branch is gated on a non-empty ``TAILSCALE_AUTHKEY``
        env var (no key → no tailnet, full backwards compatibility).
      * The branch starts ``tailscaled`` in ``--tun=userspace-networking``
        mode so the container does not need ``--privileged`` /
        ``NET_ADMIN``.
      * The ``tailscale up`` invocation passes
        ``--advertise-tags=tag:whilly-worker`` so the auth key the
        operator mints can be a tagged key (the recommended ACL shape).
      * The ``--auth-key`` value comes from the env var and is never
        echoed (we don't enable bash xtrace in this branch).

    Static parsing catches all four invariants at once.
    """
    assert ENTRYPOINT_SH.exists(), f"entrypoint.sh not found at {ENTRYPOINT_SH}"
    text = ENTRYPOINT_SH.read_text(encoding="utf-8")

    # The guard must reference TAILSCALE_AUTHKEY in a `[[ -n ... ]]`-shaped
    # check so the bootstrap is opt-in.
    assert "TAILSCALE_AUTHKEY" in text, (
        "docker/entrypoint.sh must reference TAILSCALE_AUTHKEY for the opt-in tailnet bootstrap branch."
    )
    assert '-n "${TAILSCALE_AUTHKEY' in text or '-n "$TAILSCALE_AUTHKEY' in text, (
        "docker/entrypoint.sh must guard the bootstrap on a non-empty "
        'TAILSCALE_AUTHKEY (`[[ -n "${TAILSCALE_AUTHKEY:-}" ]]`) so '
        "workers without the env continue to behave as before."
    )

    # Userspace-networking is the no-privilege mode mandated by the spec.
    assert "--tun=userspace-networking" in text, (
        "docker/entrypoint.sh must launch tailscaled with `--tun=userspace-networking` (no host TUN, no NET_ADMIN cap)."
    )

    # The tag advertisement matches the recommended ACL shape.
    assert "--advertise-tags=tag:whilly-worker" in text, (
        "docker/entrypoint.sh must run `tailscale up "
        "--advertise-tags=tag:whilly-worker` so the worker auth key the "
        "operator mints can be a tagged key."
    )

    # The auth-key flag must reference the env var (not a literal).
    assert '--auth-key="${TAILSCALE_AUTHKEY' in text or '--auth-key="$TAILSCALE_AUTHKEY' in text, (
        "docker/entrypoint.sh must pass --auth-key from the TAILSCALE_AUTHKEY env var (never a hardcoded literal)."
    )


def test_dockerfile_declares_tailscale_arg_in_both_stages() -> None:
    """Both Dockerfile stages declare ``ARG WHILLY_INCLUDE_TAILSCALE``.

    Mirrors ``test_dockerfile_agent_clis_arg.py`` for the Tailscale flag —
    catches a regression where someone removes the ARG from one stage
    but not the other, breaking the slim-build contract for a target.
    """
    assert DOCKERFILE.exists(), f"Dockerfile not found at {DOCKERFILE}"
    text = DOCKERFILE.read_text(encoding="utf-8")

    arg_count = text.count("ARG WHILLY_INCLUDE_TAILSCALE=")
    assert arg_count >= 2, (
        f"Dockerfile must declare `ARG WHILLY_INCLUDE_TAILSCALE=` in both "
        f"the runtime stage and the worker stage; found "
        f"{arg_count} declaration(s) only."
    )

    # The install RUN block must reference the arg so the build-arg is
    # actually consumed (no dead-code).
    assert "${WHILLY_INCLUDE_TAILSCALE}" in text, (
        "Dockerfile declares ARG WHILLY_INCLUDE_TAILSCALE but no RUN "
        "block references ${WHILLY_INCLUDE_TAILSCALE} — the build-arg "
        "is dead code."
    )


# ─── (2) Default build: tailscale binary present ─────────────────────────────


@DOCKER_REQUIRED
def test_default_worker_build_ships_tailscale_binary() -> None:
    """Default ``--target worker`` build ships ``tailscale --version``.

    Steps:
    (a) ``docker buildx build --target worker
        --build-arg WHILLY_INCLUDE_TAILSCALE=1 -t <tag> .`` succeeds.
    (b) ``docker run --rm --entrypoint tailscale <tag> --version`` exits
        0 with a non-empty version string.

    This proves the static-binary install in the worker stage actually
    lands ``tailscale`` on PATH for default builds.

    Cleanup runs in ``finally`` so failures don't leak the test image.
    """
    tag = DEFAULT_TAG

    build_proc = _run(
        [
            "docker",
            "buildx",
            "build",
            "--target",
            "worker",
            "--build-arg",
            "WHILLY_INCLUDE_TAILSCALE=1",
            "--load",
            "-t",
            tag,
            str(REPO_ROOT),
        ],
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if build_proc.returncode != 0:
        stdout_tail = "\n".join(build_proc.stdout.splitlines()[-40:])
        stderr_tail = "\n".join(build_proc.stderr.splitlines()[-40:])
        pytest.fail(
            f"`docker buildx build --target worker "
            f"--build-arg WHILLY_INCLUDE_TAILSCALE=1` exited "
            f"{build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        version_proc = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "tailscale",
                tag,
                "--version",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert version_proc.returncode == 0, (
            f"`tailscale --version` in default worker image exited "
            f"{version_proc.returncode}; expected 0 (binary must be on PATH "
            "when WHILLY_INCLUDE_TAILSCALE=1).\n"
            f"stdout: {version_proc.stdout!r}\n"
            f"stderr: {version_proc.stderr!r}"
        )
        assert version_proc.stdout.strip(), (
            f"`tailscale --version` exited 0 but stdout was empty: {version_proc.stdout!r}"
        )
    finally:
        _docker_rmi(tag)


# ─── (3) Slim build: tailscale binary absent ─────────────────────────────────


@DOCKER_REQUIRED
def test_slim_worker_build_omits_tailscale_binary() -> None:
    """``WHILLY_INCLUDE_TAILSCALE=0`` build leaves ``tailscale`` off PATH.

    Steps:
    (a) ``docker buildx build --target worker
        --build-arg WHILLY_INCLUDE_TAILSCALE=0 -t <tag> .`` succeeds.
    (b) ``docker run --rm --entrypoint which <tag> tailscale`` exits
        non-zero (binary not on PATH).

    Confirms the build-arg actually gates the install — an operator
    who never uses the tailnet path can shrink the image.
    """
    tag = SLIM_TAG

    build_proc = _run(
        [
            "docker",
            "buildx",
            "build",
            "--target",
            "worker",
            "--build-arg",
            "WHILLY_INCLUDE_TAILSCALE=0",
            "--load",
            "-t",
            tag,
            str(REPO_ROOT),
        ],
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if build_proc.returncode != 0:
        stdout_tail = "\n".join(build_proc.stdout.splitlines()[-40:])
        stderr_tail = "\n".join(build_proc.stderr.splitlines()[-40:])
        pytest.fail(
            f"`docker buildx build --target worker "
            f"--build-arg WHILLY_INCLUDE_TAILSCALE=0` exited "
            f"{build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        which_proc = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "which",
                tag,
                "tailscale",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert which_proc.returncode != 0, (
            f"`which tailscale` in slim worker image exited 0 "
            f"(path={which_proc.stdout!r}); expected non-zero — "
            "WHILLY_INCLUDE_TAILSCALE=0 must skip the install entirely."
        )
    finally:
        _docker_rmi(tag)
