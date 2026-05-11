"""Worker image purity regression: tailscale must NOT be present.

Backs the ``m1-tailscale-rip-out`` feature: as of the 2026-05-02
localhost.run pivot, Tailscale is removed from the architecture. The
worker stage of ``Dockerfile`` no longer ships ``tailscale`` /
``tailscaled``. This test rebuilds the ``worker`` target and asserts
the binary is absent from PATH inside the resulting image.

Skipping policy
---------------
* Skips cleanly when the Docker daemon is unreachable (``docker info``
  exits non-zero or the CLI is missing) — environment problems must
  not be reported as test failures per the mission's validator hygiene.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAG: str = "whilly-worker:tailscale-purity-test"

BUILD_TIMEOUT_SECONDS: float = 1200.0
RUN_TIMEOUT_SECONDS: float = 60.0


def _docker_available() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        proc = subprocess.run(  # noqa: S603 — literal argv
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


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — literal argv
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _docker_rmi(tag: str) -> None:
    subprocess.run(  # noqa: S603 — literal argv
        ["docker", "rmi", "-f", tag],
        capture_output=True,
        timeout=30,
        check=False,
    )


@DOCKER_REQUIRED
def test_worker_image_has_no_tailscale_binary() -> None:
    """``--target worker`` build must NOT ship ``tailscale``.

    Steps:
    (a) ``docker buildx build --target worker -t <tag> .`` succeeds.
    (b) ``docker run --rm --entrypoint which <tag> tailscale`` exits
        non-zero (binary not on PATH).

    Cleanup runs in ``finally`` so failures don't leak the test image.
    """
    build_proc = _run(
        [
            "docker",
            "buildx",
            "build",
            "--target",
            "worker",
            "--load",
            "-t",
            TAG,
            str(REPO_ROOT),
        ],
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if build_proc.returncode != 0:
        stdout_tail = "\n".join(build_proc.stdout.splitlines()[-40:])
        stderr_tail = "\n".join(build_proc.stderr.splitlines()[-40:])
        pytest.fail(
            f"`docker buildx build --target worker` exited {build_proc.returncode}.\n"
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
                TAG,
                "tailscale",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert which_proc.returncode != 0, (
            f"`which tailscale` in worker image exited 0 "
            f"(path={which_proc.stdout!r}); expected non-zero — Tailscale "
            "was removed from the worker image in the 2026-05-02 "
            "localhost.run pivot (m1-tailscale-rip-out)."
        )
    finally:
        _docker_rmi(TAG)
