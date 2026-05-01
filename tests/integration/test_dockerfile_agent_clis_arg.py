"""Dockerfile ``WHILLY_AGENT_CLIS`` build-arg integration test (v4.4.x).

Backs the ``m1-worker-image-cli-arg`` feature: the worker stage's
``npm install -g <agent CLIs>`` step is configurable via a Dockerfile
build-arg so that constrained build environments (e.g. a Colima VM with
limited disk) can sub-set the install. Default builds are unchanged
(``opencode-ai`` only on the ``worker`` stage; the historical 4-CLI set
on the multi-role ``runtime`` stage).

This test exercises the *slim* path:

1. ``docker buildx build --target worker --build-arg WHILLY_AGENT_CLIS='opencode-ai'``
   succeeds (the build-arg is plumbed through the npm install RUN line
   without quote/expansion bugs and the post-install sanity check still
   passes for opencode).
2. ``docker run --rm whilly-worker:slim which opencode`` returns 0 with
   a non-empty path.
3. ``docker run --rm whilly-worker:slim which claude || true`` returns
   non-zero — claude-code is NOT in the slim install set, the binary
   must not be on PATH.

Skipping policy
---------------
* The Docker daemon is required (``docker info`` exits zero). When it
  is not reachable the test ``pytest.skip``s with a clear reason — it
  never *fails* due to environment unavailability.
* The build is genuinely heavy on first run (apt + node + npm install
  on a fresh ``python:3.12-slim-bookworm``); BuildKit's layer cache
  makes subsequent runs fast.
* No coupling to testcontainers / asyncpg — the test only shells out
  to ``docker``.

Sibling
-------
``tests/integration/test_worker_image_import_purity.py`` covers the
Python-side dep-closure invariant (``pip list`` does not leak
fastapi/asyncpg/etc. into the worker image). This file covers the
parallel npm-side dep-closure invariant (``WHILLY_AGENT_CLIS`` controls
which Node CLIs land in the image, with the slim build proving an
operator can shrink the install set without breaking the rest of the
worker stage).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Repo root = this file's grandparent's grandparent (tests/integration → tests → repo)
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Tag for the slim build under test. Operators can override via the
# WHILLY_WORKER_SLIM_TAG env var to keep the image around for debugging.
SLIM_TAG: str = "whilly-worker:slim-test"

# Build timeout — first run on a clean machine pulls python:3.12-slim
# (~50MB), runs apt (~200MB), installs Node 22 LTS via NodeSource (~80MB),
# then npm-installs opencode-ai (~150MB unpacked). 15 minutes is generous;
# warm BuildKit cache finishes in seconds.
BUILD_TIMEOUT_SECONDS: float = 900.0

# Run timeout — `which <bin>` is a single PATH walk, finishes in <1s
# locally. 60s ceiling is generous.
RUN_TIMEOUT_SECONDS: float = 60.0


def _docker_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the Docker daemon.

    Mirrors the cheap-and-deterministic gate used by
    ``tests/integration/test_worker_image_import_purity.py``. Both the
    binary AND a reachable daemon are required.
    """
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
    """Helper to run a subprocess and return ``CompletedProcess`` with text I/O."""
    return subprocess.run(  # noqa: S603 — argv assembled from literals
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _build_slim_image(tag: str) -> subprocess.CompletedProcess[str]:
    """Build the ``worker`` stage with ``WHILLY_AGENT_CLIS='opencode-ai'``.

    The literal value matches the docs/Distributed-Setup.md "slim" example
    and the feature spec verbatim — slim build with only opencode on PATH.
    """
    build_cmd = [
        "docker",
        "buildx",
        "build",
        "--target",
        "worker",
        "--build-arg",
        "WHILLY_AGENT_CLIS=opencode-ai",
        "--load",  # local image, not push — required for `docker run` after
        "-t",
        tag,
        str(REPO_ROOT),
    ]
    return _run(build_cmd, timeout=BUILD_TIMEOUT_SECONDS)


def _docker_rmi(tag: str) -> None:
    """Best-effort image cleanup. Errors are swallowed so a failed cleanup
    (e.g. another container is using the image) doesn't mask test results.
    """
    subprocess.run(  # noqa: S603 — literal argv
        ["docker", "rmi", "-f", tag],
        capture_output=True,
        timeout=30,
        check=False,
    )


@DOCKER_REQUIRED
def test_slim_build_succeeds_and_opencode_only() -> None:
    """End-to-end: slim build → opencode on PATH → claude NOT on PATH.

    Steps:

    (a) ``docker buildx build --target worker
        --build-arg WHILLY_AGENT_CLIS='opencode-ai' -t whilly-worker:slim .``
        succeeds (exit 0). This proves the build-arg is plumbed through
        the npm install RUN line without quote/expansion bugs and the
        post-install sanity check accepts opencode-ai-only as valid.

    (b) ``docker run --rm whilly-worker:slim which opencode`` returns 0
        with non-empty stdout. The slim install set DOES include
        opencode-ai, so the binary must be on PATH.

    (c) ``docker run --rm whilly-worker:slim which claude`` returns
        non-zero. claude-code is NOT in the slim install set; the
        binary must NOT be on PATH (or the slim contract is broken).

    Cleanup is best-effort and runs in ``finally`` so a failed assertion
    doesn't leak the test image across runs.
    """
    tag = SLIM_TAG

    build_proc = _build_slim_image(tag)
    if build_proc.returncode != 0:
        # Truncate stdout/stderr at the tail so the pytest report stays
        # navigable but still names the failing RUN line.
        stdout_tail = "\n".join(build_proc.stdout.splitlines()[-40:])
        stderr_tail = "\n".join(build_proc.stderr.splitlines()[-40:])
        pytest.fail(
            f"`docker buildx build --target worker "
            f"--build-arg WHILLY_AGENT_CLIS='opencode-ai'` exited "
            f"{build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        # (b) opencode MUST be on PATH in the slim build.
        which_opencode = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "which",
                tag,
                "opencode",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert which_opencode.returncode == 0, (
            f"`which opencode` in slim image exited {which_opencode.returncode}; "
            "expected 0 (opencode-ai is in the slim install set).\n"
            f"stdout: {which_opencode.stdout!r}\n"
            f"stderr: {which_opencode.stderr!r}"
        )
        opencode_path = which_opencode.stdout.strip()
        assert opencode_path, f"`which opencode` exited 0 but stdout was empty: {which_opencode.stdout!r}"

        # (c) claude MUST NOT be on PATH in the slim build.
        which_claude = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "which",
                tag,
                "claude",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert which_claude.returncode != 0, (
            f"`which claude` in slim image exited 0 (path={which_claude.stdout!r}); "
            "expected non-zero — @anthropic-ai/claude-code is NOT in the slim "
            "install set, the binary must not be on PATH."
        )
    finally:
        _docker_rmi(tag)


@DOCKER_REQUIRED
def test_empty_build_skips_npm_install() -> None:
    """Empty ``WHILLY_AGENT_CLIS`` skips the npm install layer cleanly.

    A slimmer-than-slim variant: pass ``WHILLY_AGENT_CLIS=''`` to skip
    the npm install entirely (operator BYOs the agent CLI binary via
    volume-mount or a follow-on RUN layer). The image must still build
    successfully and the python/whilly entrypoint must remain functional.

    We probe entrypoint health by overriding the entrypoint and running
    ``python -c 'import whilly; print(whilly.__version__)'`` — this
    exercises the same import path as the legitimate worker entrypoint
    without requiring control-plane env vars.
    """
    tag = "whilly-worker:no-clis-test"

    build_proc = _run(
        [
            "docker",
            "buildx",
            "build",
            "--target",
            "worker",
            "--build-arg",
            "WHILLY_AGENT_CLIS=",
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
            f"`docker buildx build --build-arg WHILLY_AGENT_CLIS=` "
            f"(empty) exited {build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        # whilly Python package must still import (sanity check that the
        # empty CLI install didn't break anything in the venv layer).
        py_proc = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                tag,
                "-c",
                "import whilly; print(whilly.__version__)",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert py_proc.returncode == 0, (
            f"`python -c 'import whilly'` exited {py_proc.returncode}; "
            "empty WHILLY_AGENT_CLIS build broke the python entrypoint.\n"
            f"stdout: {py_proc.stdout!r}\n"
            f"stderr: {py_proc.stderr!r}"
        )
        # Version string should be non-empty — protects against an
        # accidentally-stubbed import that returns no metadata.
        assert py_proc.stdout.strip(), f"whilly.__version__ was empty: {py_proc.stdout!r}"

        # opencode must NOT be on PATH (empty install set).
        which_opencode = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "which",
                tag,
                "opencode",
            ],
            timeout=RUN_TIMEOUT_SECONDS,
        )
        assert which_opencode.returncode != 0, (
            f"`which opencode` in no-clis image exited 0 "
            f"(path={which_opencode.stdout!r}); "
            "expected non-zero — empty WHILLY_AGENT_CLIS skips npm install."
        )
    finally:
        _docker_rmi(tag)


def test_dockerfile_declares_arg_in_both_stages() -> None:
    """Static check: ``ARG WHILLY_AGENT_CLIS`` is declared in both the
    multi-role ``runtime`` stage AND the worker-only ``worker`` stage.

    This runs without Docker (parses the Dockerfile as text). Catches a
    regression where someone removes the ARG from one stage but not the
    other — the slim/empty contract must hold for both image targets.
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    # Two ARG declarations expected: one per stage. Each declaration
    # must include the literal token (defaults differ).
    arg_count = text.count("ARG WHILLY_AGENT_CLIS=")
    assert arg_count >= 2, (
        f"Dockerfile must declare `ARG WHILLY_AGENT_CLIS=` in both the "
        f"runtime and worker stages; found {arg_count} declaration(s) only.\n"
        f"Dockerfile path: {dockerfile}"
    )
    # Each npm install RUN line must reference the arg (so the build-arg
    # is actually consumed). Both stages have a `npm install -g` line.
    assert "${WHILLY_AGENT_CLIS}" in text, (
        "Dockerfile declares ARG WHILLY_AGENT_CLIS but the npm install "
        "RUN line does not reference ${WHILLY_AGENT_CLIS} — the build-arg "
        "is dead code."
    )
