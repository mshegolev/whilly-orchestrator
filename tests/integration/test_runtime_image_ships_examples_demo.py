"""Runtime image ships ``examples/demo/`` plans (VAL-M1-COMPOSE-011).

Round-6 user-testing-validator caught that ``WHILLY_IMAGE_TAG=4.4.1
bash workshop-demo.sh --cli stub`` failed at the plan-import step
because the published ``mshegolev/whilly:4.4.1`` runtime image did not
ship ``/opt/whilly/examples/demo/parallel.json``. ``workshop-demo.sh``
shells ``docker compose exec -T control-plane whilly plan import
"$PLAN_FILE"`` where ``$PLAN_FILE`` lives at the container WORKDIR
(``/opt/whilly/examples/demo/...``); without those files the demo
cannot drain its 5 tasks.

Root cause: the production ``Dockerfile`` runtime stage deliberately
excluded ``examples/`` (per the leading comment "Не тащит в runtime
тесты, fixtures, examples/, README'ы"). ``Dockerfile.demo`` had the
``COPY examples/`` directive but the multi-arch image used for the
public Docker Hub tag did not.

Fix: the runtime stage now copies ``examples/`` into
``/opt/whilly/examples/`` (~10 KB total — negligible). This test is
the install-time regression gate that backs that contract.

Steps:
  1. ``docker buildx build --target runtime -t <tag> .`` from the
     repo root.
  2. ``docker run --rm --entrypoint ls <tag> /opt/whilly/examples/demo``
     so we don't trip the role dispatcher's
     ``WHILLY_DATABASE_URL`` requirement.
  3. Assert ``parallel.json``, ``tasks.json``, and ``PRD-demo.md`` all
     appear in the listing.
  4. Best-effort ``docker rmi`` cleanup.

Skipping policy mirrors
``tests/integration/test_worker_image_import_purity.py`` and
``tests/integration/test_dockerfile_default_target_cmd.py``: a
missing / unreachable Docker daemon ``pytest.skip``s rather than
fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

DEFAULT_TAG: str = "whilly-runtime:examples-test"

REQUIRED_FILES: tuple[str, ...] = (
    "parallel.json",
    "tasks.json",
    "PRD-demo.md",
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


def test_dockerfile_runtime_stage_copies_examples() -> None:
    """Static check (always runs): runtime stage in Dockerfile copies examples/.

    This is the cheap PR-review-time gate. The Docker-gated check below
    proves the actual image contents.
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    assert dockerfile.is_file(), f"Dockerfile not found at {dockerfile}"
    text = dockerfile.read_text(encoding="utf-8")

    runtime_idx = text.find("AS runtime")
    assert runtime_idx != -1, "Dockerfile is missing the `AS runtime` stage marker"
    runtime_stage = text[runtime_idx:]

    assert (
        "COPY examples /opt/whilly/examples/" in runtime_stage
        or "COPY examples/ /opt/whilly/examples/" in runtime_stage
    ), (
        "Dockerfile runtime stage does not COPY examples/ into /opt/whilly/examples/. "
        "Without these files, `workshop-demo.sh` fails at the `whilly plan import "
        '"$PLAN_FILE"` step because the demo plans live at /opt/whilly/examples/demo/. '
        "See VAL-M1-COMPOSE-011 (Round 6 user-testing finding)."
    )


def test_repo_examples_demo_files_present() -> None:
    """Sanity gate: ensure the source files we expect to ship actually exist.

    Catches an earlier-stage regression where someone deletes one of the
    demo fixtures from the repo. If this test fails, the
    Docker-gated test below would also fail with a misleading
    "image is missing the file" error.
    """
    demo_dir = REPO_ROOT / "examples" / "demo"
    for fname in REQUIRED_FILES:
        assert (demo_dir / fname).is_file(), (
            f"Required demo fixture missing from repo: examples/demo/{fname}. "
            "workshop-demo.sh and the runtime image both depend on these files."
        )


@DOCKER_REQUIRED
def test_runtime_image_ships_examples_demo_files() -> None:
    """`docker buildx build --target runtime` produces an image that ships the demo plans.

    Mirrors the static test above with a real image build + filesystem
    listing inside the container. This is the canonical contract — the
    text-only check is a PR-review accelerator, not a substitute.
    """
    tag = DEFAULT_TAG

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
                "/opt/whilly/examples/demo",
            ],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
        assert ls_proc.returncode == 0, (
            f"`docker run ls /opt/whilly/examples/demo` exited {ls_proc.returncode}. "
            "The runtime image is missing the examples/demo directory entirely. "
            "Fix: add `COPY examples /opt/whilly/examples/` to the runtime stage of Dockerfile.\n"
            f"stdout:\n{ls_proc.stdout}\n"
            f"stderr:\n{ls_proc.stderr}"
        )

        listed = {line.strip() for line in ls_proc.stdout.splitlines() if line.strip()}
        missing = sorted(set(REQUIRED_FILES) - listed)
        assert not missing, (
            "Runtime image is missing required demo fixtures under "
            f"/opt/whilly/examples/demo: {missing}. workshop-demo.sh's "
            '`whilly plan import "$PLAN_FILE"` step depends on these files.\n'
            f"--- ls output ---\n{ls_proc.stdout}"
        )
    finally:
        subprocess.run(  # noqa: S603 — fully literal argv
            ["docker", "rmi", "-f", tag],
            capture_output=True,
            timeout=30,
            check=False,
        )
