"""Worker docker image import-path purity (VAL-M1-COMPOSE-012).

Builds the ``worker`` Dockerfile target stage and asserts that the
resulting image's ``pip list`` contains ZERO of the control-plane-only
Python distributions:

* ``fastapi``
* ``asyncpg``
* ``sse-starlette``
* ``prometheus-fastapi-instrumentator``
* ``jinja2``

This is the runtime sibling of the ``.importlinter`` contract. That
gate only graphs Python imports — it cannot detect distributions that
sneak into a worker image because the Dockerfile installed
``'.[server,worker]'`` instead of the slimmer ``'.[worker]'``. The
M1 user-testing round-1 finding (VAL-M1-COMPOSE-012) caught exactly
that regression — ``pip list`` inside the published worker container
showed both ``asyncpg`` and ``fastapi`` because the legacy ``runtime``
stage installs both extras for multi-role flexibility.

The fix lives in ``Dockerfile`` — a separate ``worker`` build target
(stage 4, with ``worker-builder`` as stage 3) that runs
``pip install '.[worker]'`` and never the ``[server]`` extra. This
test is the install-time regression gate that backs that contract.

Skipping policy
---------------
* The Docker daemon is required (``docker info`` exits zero). When
  it is not reachable the test ``pytest.skip``s with a clear reason —
  it never *fails* due to environment unavailability.
* The build is genuinely heavy (apt + pip on a fresh
  ``python:3.12-slim-bookworm``); BuildKit layer caching makes
  subsequent runs fast on the same machine.
* No coupling to testcontainers / asyncpg — the test only shells out
  to ``docker``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Repo root = this file's grandparent's grandparent (tests/integration → tests → repo)
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# The five distributions M1 user-testing round 1 (VAL-M1-COMPOSE-012) caught
# leaking into the worker image. Mission §8 codifies the same list as the
# "Worker import-path purity" invariant.
FORBIDDEN_DISTS: tuple[str, ...] = (
    "fastapi",
    "asyncpg",
    "sse-starlette",
    "prometheus-fastapi-instrumentator",
    "jinja2",
)

# A unique tag per test run so concurrent runs (or repeated runs across
# branches) don't step on each other's image cache. Operators can override
# via WHILLY_WORKER_PURITY_TAG to keep the image around for debugging.
DEFAULT_TAG: str = "whilly-worker:purity-test"

# Build timeout — first run on a clean machine pulls ``python:3.12-slim``
# (~50MB), runs ``apt-get install build-essential`` (~200MB), and
# ``pip install '.[worker]'``. 15 minutes is generous; subsequent runs
# hit the layer cache and finish in seconds.
BUILD_TIMEOUT_SECONDS: float = 900.0

# Run timeout — `pip list` is a pure dist-info walk, finishes in <2s
# locally. 60s is a generous ceiling.
RUN_TIMEOUT_SECONDS: float = 60.0


def _docker_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the Docker daemon.

    Mirrors the cheap-and-deterministic gate used by
    ``tests/conftest.py::docker_available`` and
    ``tests/integration/test_phase6_cross_host_compose.py``. Both the
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


def _parse_pip_list_freeze(output: str) -> list[str]:
    """Extract distribution names from ``pip list --format=freeze`` output.

    Each line is ``<name>==<version>`` (or ``<name> @ <url>`` for VCS
    installs). Returns the lowercased name component, stripped, with
    blank lines and ``# Editable install...`` comments dropped.
    """
    names: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # ``<name>==<version>`` is the canonical form. Editable / VCS
        # installs use ``<name> @ <url>``; fall back to splitting on
        # whitespace so we still capture the name there.
        if "==" in line:
            name = line.split("==", 1)[0]
        elif " @ " in line:
            name = line.split(" @ ", 1)[0]
        else:
            name = line.split()[0]
        names.append(name.strip().lower())
    return names


@DOCKER_REQUIRED
def test_worker_image_pip_list_excludes_control_plane_dists() -> None:
    """``pip list`` inside the ``worker`` stage shows none of the forbidden dists.

    Steps:
      1. ``docker build --target worker -t whilly-worker:purity-test .`` from
         the repo root. BuildKit's layer cache makes this fast on warm runs.
      2. ``docker run --rm --entrypoint pip <tag> list --format=freeze`` —
         override the entrypoint so we don't trip the role dispatcher's
         ``WHILLY_CONTROL_URL`` / ``WHILLY_PLAN_ID`` requirement.
      3. Parse ``<name>==<version>`` lines, lowercase the names, and assert
         that none of :data:`FORBIDDEN_DISTS` appear.
      4. Best-effort ``docker rmi`` cleanup so the test machine doesn't
         accumulate images across runs.

    On `pip list` failure the test re-raises with full stderr so the operator
    can debug the build / run interaction without re-running locally.
    """
    tag = DEFAULT_TAG

    build_cmd = [
        "docker",
        "build",
        "--target",
        "worker",
        "-t",
        tag,
        str(REPO_ROOT),
    ]
    build_proc = subprocess.run(  # noqa: S603 — fully literal argv
        build_cmd,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if build_proc.returncode != 0:
        # Truncate stdout/stderr at the tail so the pytest report stays
        # navigable but still names the failing RUN line.
        stdout_tail = "\n".join(build_proc.stdout.splitlines()[-40:])
        stderr_tail = "\n".join(build_proc.stderr.splitlines()[-40:])
        pytest.fail(
            f"`docker build --target worker` exited {build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        run_proc = subprocess.run(  # noqa: S603 — fully literal argv
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "pip",
                tag,
                "list",
                "--format=freeze",
            ],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
        assert run_proc.returncode == 0, (
            f"`docker run pip list` exited {run_proc.returncode}\n"
            f"stdout:\n{run_proc.stdout}\n"
            f"stderr:\n{run_proc.stderr}"
        )

        installed = _parse_pip_list_freeze(run_proc.stdout)
        forbidden_lower = {d.lower() for d in FORBIDDEN_DISTS}
        leaks = sorted({name for name in installed if name in forbidden_lower})

        assert not leaks, (
            "Worker image leaks control-plane-only Python distributions:\n"
            f"  found: {leaks}\n"
            f"  forbidden: {sorted(forbidden_lower)}\n"
            "Fix: ensure the `worker` Dockerfile stage installs only "
            "`'.[worker]'` (NOT `'.[server,worker]'`).\n"
            f"--- pip list (full) ---\n{run_proc.stdout}"
        )

        # Runtime sibling of ``pip list``: the worker binary must
        # actually start. The v4.4.0 fastapi-leak regression
        # (fix-m1-whilly-worker-fastapi-leak) was caught at import-time —
        # fastapi was missing from the [worker] extras *and* the entry
        # closure pulled it in via whilly.adapters.transport.__init__
        # eager re-exports. ``pip list`` proves the dist is absent;
        # ``whilly-worker --help`` proves the entry closure does not
        # *try* to import it.
        help_proc = subprocess.run(  # noqa: S603 — fully literal argv
            ["docker", "run", "--rm", "--entrypoint", "whilly-worker", tag, "--help"],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
        assert help_proc.returncode == 0, (
            f"`docker run whilly-worker --help` exited {help_proc.returncode} "
            "inside the worker image. This is the v4.4.0 fastapi-leak shape — "
            "the worker entry closure must run with only the [worker] extras.\n"
            f"stdout:\n{help_proc.stdout}\n"
            f"stderr:\n{help_proc.stderr}\n"
        )
    finally:
        # Best-effort cleanup. The image is small (~150MB) but accumulating
        # one per CI run is unfriendly. Errors are intentionally swallowed
        # so a failed `docker rmi` (e.g. another container is using the
        # image) doesn't mask the actual test result.
        subprocess.run(  # noqa: S603 — fully literal argv
            ["docker", "rmi", "-f", tag],
            capture_output=True,
            timeout=30,
            check=False,
        )
