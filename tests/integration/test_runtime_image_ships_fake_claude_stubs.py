"""Runtime image ships ``tests/fixtures/fake_claude*.sh`` stubs (VAL-M1-COMPOSE-011).

Round-6 v4.4.2 publish smoke (worker session 1f990ed6) found that
``WHILLY_IMAGE_TAG=4.4.2 bash workshop-demo.sh --cli stub`` still failed
end-to-end against the published ``mshegolev/whilly:4.4.2`` runtime
image: workers claim tasks but immediately fail with
``claude binary not found at /opt/whilly/tests/fixtures/fake_claude_demo.sh``.

Root cause: the ``Dockerfile`` runtime stage (used for the public
``mshegolev/whilly:<version>`` image) shipped ``examples/`` (added by
commit ``02fc9f2``) but NOT the fake_claude stub fixtures.
``Dockerfile.demo`` (locally-built ``whilly-demo:latest``) already ships
them via these directives:

    COPY tests/fixtures/fake_claude.sh ./tests/fixtures/fake_claude.sh
    COPY tests/fixtures/fake_claude_demo.sh ./tests/fixtures/fake_claude_demo.sh

This test is the install-time regression gate ensuring the production
runtime image now mirrors that contract.

Steps:
  1. Static text scan: confirm both COPY directives are present in the
     runtime stage and that the stage's chmod block sets the executable
     bit on both files.
  2. Repo-level sanity: confirm the source files exist and are
     executable on disk.
  3. Docker-gated check: ``docker buildx build --target runtime`` then
     ``docker run --rm --entrypoint ls`` to assert both files appear
     under ``/opt/whilly/tests/fixtures/`` and ``test -x`` succeeds for
     each.
  4. Best-effort ``docker rmi`` cleanup.

Skipping policy mirrors ``test_runtime_image_ships_examples_demo.py``:
a missing/unreachable Docker daemon ``pytest.skip``s rather than fails.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

DEFAULT_TAG: str = "whilly-runtime:fake-claude-stubs-test"

REQUIRED_STUBS: tuple[str, ...] = (
    "fake_claude.sh",
    "fake_claude_demo.sh",
)

BUILD_TIMEOUT_SECONDS: float = 1500.0
RUN_TIMEOUT_SECONDS: float = 60.0


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


def test_dockerfile_runtime_stage_copies_fake_claude_stubs() -> None:
    """Static check (always runs): runtime stage in Dockerfile copies the stubs.

    Cheap PR-review-time gate. The Docker-gated check below proves the
    actual image contents.
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    assert dockerfile.is_file(), f"Dockerfile not found at {dockerfile}"
    text = dockerfile.read_text(encoding="utf-8")

    runtime_idx = text.find("AS runtime")
    assert runtime_idx != -1, "Dockerfile is missing the `AS runtime` stage marker"
    runtime_stage = text[runtime_idx:]

    expected_copies = (
        "COPY tests/fixtures/fake_claude.sh /opt/whilly/tests/fixtures/fake_claude.sh",
        "COPY tests/fixtures/fake_claude_demo.sh /opt/whilly/tests/fixtures/fake_claude_demo.sh",
    )
    for directive in expected_copies:
        assert directive in runtime_stage, (
            "Dockerfile runtime stage is missing the COPY directive: "
            f"`{directive}`. Without this, `workshop-demo.sh --cli stub` fails at "
            "the worker step with `claude binary not found at "
            "/opt/whilly/tests/fixtures/fake_claude_demo.sh`. "
            "See VAL-M1-COMPOSE-011 (Round-6 v4.4.2 publish finding)."
        )

    for stub in REQUIRED_STUBS:
        assert f"/opt/whilly/tests/fixtures/{stub}" in runtime_stage, (
            f"Runtime stage chmod block is missing /opt/whilly/tests/fixtures/{stub}. "
            "The stub must be marked executable in the image so docker-compose's "
            "`worker` service can exec it as CLAUDE_BIN."
        )


def test_repo_fake_claude_stub_files_executable() -> None:
    """Sanity gate: ensure the source stubs exist and are executable on disk.

    Catches an earlier-stage regression where someone clears the +x bit
    or deletes a fixture from the repo. If this test fails, the
    Docker-gated test below would also fail with a misleading
    "image is missing the file" error.
    """
    fixtures_dir = REPO_ROOT / "tests" / "fixtures"
    for fname in REQUIRED_STUBS:
        path = fixtures_dir / fname
        assert path.is_file(), (
            f"Required stub missing from repo: tests/fixtures/{fname}. "
            "workshop-demo.sh and the runtime image both depend on this file."
        )
        mode = path.stat().st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        assert is_exec, (
            f"tests/fixtures/{fname} is not executable on disk (mode={oct(mode)}). "
            f"Restore the +x bit so the runtime image inherits it: "
            f"`chmod +x tests/fixtures/{fname}`."
        )


@DOCKER_REQUIRED
def test_runtime_image_ships_fake_claude_stubs_executable() -> None:
    """`docker buildx build --target runtime` produces an image that ships the stubs as executable.

    Mirrors the static test above with a real image build + filesystem
    listing inside the container plus a `test -x` probe. This is the
    canonical contract — the text-only check is a PR-review accelerator,
    not a substitute.
    """
    tag = os.environ.get("WHILLY_TEST_RUNTIME_TAG", DEFAULT_TAG)

    build_cmd = [
        "docker",
        "buildx",
        "build",
        "--load",
        "--target",
        "runtime",
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
            f"`docker buildx build --target runtime` exited {build_proc.returncode}.\n"
            f"--- stdout (last 40 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 40 lines) ---\n{stderr_tail}\n"
        )

    try:
        ls_proc = subprocess.run(  # noqa: S603 — fully literal argv
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "ls",
                tag,
                "/opt/whilly/tests/fixtures",
            ],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
        assert ls_proc.returncode == 0, (
            f"`docker run ls /opt/whilly/tests/fixtures` exited {ls_proc.returncode}. "
            "The runtime image is missing the tests/fixtures directory entirely. "
            "Fix: add the COPY tests/fixtures/fake_claude*.sh directives to the "
            "runtime stage of Dockerfile.\n"
            f"stdout:\n{ls_proc.stdout}\n"
            f"stderr:\n{ls_proc.stderr}"
        )

        listed = {line.strip() for line in ls_proc.stdout.splitlines() if line.strip()}
        missing = sorted(set(REQUIRED_STUBS) - listed)
        assert not missing, (
            "Runtime image is missing required fake_claude stubs under "
            f"/opt/whilly/tests/fixtures: {missing}. workshop-demo.sh's "
            "`--cli stub` path depends on these files via CLAUDE_BIN.\n"
            f"--- ls output ---\n{ls_proc.stdout}"
        )

        for stub in REQUIRED_STUBS:
            target = f"/opt/whilly/tests/fixtures/{stub}"
            test_proc = subprocess.run(  # noqa: S603 — fully literal argv
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "test",
                    tag,
                    "-x",
                    target,
                ],
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_SECONDS,
                check=False,
            )
            assert test_proc.returncode == 0, (
                f"`test -x {target}` exited {test_proc.returncode} inside the runtime image. "
                "The stub is present but not executable — the runtime stage's chmod "
                "block must set +x on it. Without the executable bit, docker-compose's "
                "`worker` service cannot exec CLAUDE_BIN.\n"
                f"stdout:\n{test_proc.stdout}\n"
                f"stderr:\n{test_proc.stderr}"
            )
    finally:
        subprocess.run(  # noqa: S603 — fully literal argv
            ["docker", "rmi", "-f", tag],
            capture_output=True,
            timeout=30,
            check=False,
        )
