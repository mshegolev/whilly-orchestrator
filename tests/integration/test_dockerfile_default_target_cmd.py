"""Default Dockerfile build target CMD regression (`fix-m1-dockerfile-cmd-regression`).

Locks in the invariant that ``docker buildx build .`` (no ``--target``) of
the canonical ``Dockerfile`` produces an image whose ``Config.Cmd`` is
``["control-plane"]``. Background:

The published ``mshegolev/whilly:4.4.0`` shipped with
``Config.Cmd=["worker"]`` because commit ``7ae66b7`` appended new
``worker-builder`` and ``worker`` build stages AFTER the ``runtime``
stage. ``docker/build-push-action@v6`` was invoked without a ``target:``
key, so the default-target convention (last ``FROM ... AS ...`` stage)
silently shifted to ``worker``.

This test backs three layers of defence:

1. **Static check** (always runs, no Docker required): parse the
   Dockerfile, find the LAST ``FROM ... AS <name>`` line, assert
   ``<name> == "runtime"``. Catches the regression at PR review time.
2. **Workflow pin check** (always runs): parse
   ``.github/workflows/docker-publish.yml`` and assert
   ``target: runtime`` is pinned in the ``docker/build-push-action``
   step. Defence-in-depth: even if the stage order accidentally
   changes again, CI keeps publishing the correct image.
3. **Docker-gated check** (skips if no daemon):
   ``docker buildx build -t whilly-default-target-test .`` (no
   ``--target``) followed by ``docker inspect ... --format
   '{{json .Config.Cmd}}'`` and assert exact equality to
   ``["control-plane"]``. Same shape as the published-image check.

Skipping policy mirrors
``tests/integration/test_worker_image_import_purity.py``: a missing /
unreachable Docker daemon ``pytest.skip``s rather than fails. The
static + workflow checks always run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DOCKERFILE: Path = REPO_ROOT / "Dockerfile"
DOCKER_PUBLISH_WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"

DEFAULT_TARGET_TAG: str = "whilly-default-target-test:cmd"

BUILD_TIMEOUT_SECONDS: float = 1500.0
INSPECT_TIMEOUT_SECONDS: float = 30.0


def _docker_available() -> tuple[bool, str]:
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


_FROM_AS_RE = re.compile(
    r"^FROM\s+\S+\s+AS\s+([A-Za-z0-9_.\-]+)\s*$",
    re.IGNORECASE,
)


def _last_from_as_stage(dockerfile_text: str) -> str | None:
    last: str | None = None
    for raw in dockerfile_text.splitlines():
        m = _FROM_AS_RE.match(raw.strip())
        if m:
            last = m.group(1)
    return last


def test_last_dockerfile_stage_is_runtime() -> None:
    """The last ``FROM ... AS <name>`` line must name ``runtime``.

    Regression guard for the v4.4.0 incident where ``worker-builder`` /
    ``worker`` stages were appended after ``runtime``, silently shifting
    the default build target.
    """
    assert DOCKERFILE.is_file(), f"Dockerfile not found at {DOCKERFILE}"
    text = DOCKERFILE.read_text(encoding="utf-8")
    last = _last_from_as_stage(text)
    assert last is not None, "No `FROM ... AS <name>` lines found in Dockerfile; cannot verify default build target."
    assert last == "runtime", (
        f"The last `FROM ... AS <name>` stage in {DOCKERFILE} is `{last}`, "
        "but it MUST be `runtime` so that `docker buildx build .` (no --target) "
        "publishes the multi-role control-plane image. Fix: move any stage "
        "appended after `runtime` to come BEFORE it.\n"
        "Background: published mshegolev/whilly:4.4.0 shipped with "
        'Config.Cmd=["worker"] because commit 7ae66b7 appended worker-builder + '
        "worker stages after `runtime`."
    )


def test_docker_publish_workflow_pins_runtime_target() -> None:
    """``.github/workflows/docker-publish.yml`` must pin ``target: runtime``.

    Defence-in-depth against accidental stage-order regressions in the
    Dockerfile. Pure text-scan; tolerates indentation and surrounding
    YAML structure variation.
    """
    assert DOCKER_PUBLISH_WORKFLOW.is_file(), f"docker-publish.yml not found at {DOCKER_PUBLISH_WORKFLOW}"
    text = DOCKER_PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "build-push-action" in text, (
        f"{DOCKER_PUBLISH_WORKFLOW} does not invoke docker/build-push-action; "
        "the regression-guard assumption no longer holds. Update this test "
        "if the workflow has been intentionally restructured."
    )

    pin_re = re.compile(r"^\s*target:\s*runtime\s*$", re.MULTILINE)
    assert pin_re.search(text) is not None, (
        f"{DOCKER_PUBLISH_WORKFLOW} does not pin `target: runtime` in the "
        "docker/build-push-action step. Add a `target: runtime` line under "
        "the `with:` block of the build step so the default-target stage "
        "is fixed regardless of Dockerfile stage order."
    )


@DOCKER_REQUIRED
def test_default_target_image_cmd_is_control_plane() -> None:
    """``docker buildx build .`` (no --target) → ``Config.Cmd == ["control-plane"]``.

    Mirrors the production CI invocation in
    ``.github/workflows/docker-publish.yml`` minus the ``--push`` and
    multi-arch flags. Build is kept to the host architecture only so the
    test is fast on warm BuildKit caches.
    """
    tag = DEFAULT_TARGET_TAG

    build_cmd = [
        "docker",
        "buildx",
        "build",
        "--load",
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
        stdout_tail = "\n".join(build_proc.stdout.splitlines()[-40:])
        stderr_tail = "\n".join(build_proc.stderr.splitlines()[-40:])
        pytest.fail(
            f"`docker buildx build` (no --target) exited {build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        inspect_proc = subprocess.run(  # noqa: S603 — fully literal argv
            [
                "docker",
                "inspect",
                tag,
                "--format",
                "{{json .Config.Cmd}}",
            ],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
        assert inspect_proc.returncode == 0, (
            f"`docker inspect {tag}` exited {inspect_proc.returncode}\n"
            f"stdout: {inspect_proc.stdout!r}\n"
            f"stderr: {inspect_proc.stderr!r}"
        )
        cmd_json = inspect_proc.stdout.strip()
        cmd_value = json.loads(cmd_json)
        assert cmd_value == ["control-plane"], (
            f"Default-target image Config.Cmd is {cmd_value!r}; "
            'expected ["control-plane"]. The published mshegolev/whilly:4.4.0 '
            'shipped with ["worker"] because the worker stages were appended '
            "after `runtime`. Reorder so `runtime` is the LAST `FROM ... AS ...` line."
        )
    finally:
        subprocess.run(  # noqa: S603 — fully literal argv
            ["docker", "rmi", "-f", tag],
            capture_output=True,
            timeout=30,
            check=False,
        )
