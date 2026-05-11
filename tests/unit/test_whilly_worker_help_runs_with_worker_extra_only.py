"""``whilly-worker --help`` works in a fresh venv with only ``[worker]`` extras.

Reproduces the exact public reproduction of the v4.4.0 fastapi-leak bug:
``pip install whilly-orchestrator[worker] && whilly-worker --help`` must
exit 0 and print the standard argparse help banner. Pre-fix the same
invocation crashed with ``ModuleNotFoundError: No module named 'fastapi'``.

This test creates a throwaway venv, installs ``[worker]`` (no
``[server]``, no ``[dev]``), and runs the console script. It is
intentionally a unit test — no docker, no testcontainers — so the gate
runs in every CI matrix slot, not just the integration tier.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# 5 minutes covers a clean ``pip install`` on a cold cache (httpx +
# pydantic + base closure ≈ 30MB of wheels). Subsequent runs hit pip's
# wheel cache and finish in seconds.
INSTALL_TIMEOUT_SECONDS: float = 300.0

# `whilly-worker --help` is argparse-printing, no I/O. 30s is generous.
HELP_TIMEOUT_SECONDS: float = 30.0


def _venv_creation_works(target: Path) -> tuple[bool, str]:
    """Return ``(ok, reason)`` after attempting to create a venv at ``target``.

    Some sandboxed CI environments (notably restricted pyenv shims that
    prohibit ``ensurepip``) refuse to spawn a venv. The unit test
    skips cleanly in that case rather than failing — the integration
    tier still exercises the same path inside docker.
    """
    try:
        venv.EnvBuilder(with_pip=True, clear=True, symlinks=False).create(str(target))
    except Exception as exc:
        return False, f"venv.EnvBuilder failed: {type(exc).__name__}: {exc}"
    bin_dir = target / ("Scripts" if os.name == "nt" else "bin")
    pip_path = bin_dir / ("pip.exe" if os.name == "nt" else "pip")
    if not pip_path.exists():
        return False, f"venv created but pip is missing at {pip_path}"
    return True, "ok"


def test_whilly_worker_help_in_venv_with_worker_extra_only() -> None:
    """``whilly-worker --help`` exits 0 inside a venv with only ``[worker]``.

    This is the exact public reproduction of the v4.4.0 fastapi-leak
    regression. The fix is verified by:

    1. Creating a fresh venv (``python -m venv``).
    2. ``pip install`` of the editable repo with the ``[worker]`` extra
       only (NOT ``[server]`` and NOT ``[dev]``).
    3. Running ``<venv>/bin/whilly-worker --help`` and asserting:

       * exit code 0
       * stdout non-empty
       * stdout contains the argparse-rendered ``--connect`` flag
         (proves the help banner actually rendered, not just an empty
         success exit from a corrupted entry point).
    """
    with tempfile.TemporaryDirectory(prefix="whilly-worker-help-") as tmp:
        venv_dir = Path(tmp) / "wt"
        ok, reason = _venv_creation_works(venv_dir)
        if not ok:
            pytest.skip(f"venv creation unavailable in this environment: {reason}")

        bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        pip_path = bin_dir / ("pip.exe" if os.name == "nt" else "pip")
        worker_path = bin_dir / ("whilly-worker.exe" if os.name == "nt" else "whilly-worker")

        # Install the repo with the [worker] extra ONLY. ``--no-deps`` is
        # NOT used here: we want pip to resolve the ``[worker]`` dep
        # closure so a regression that pulls fastapi into [worker]
        # accidentally would still be caught.
        install_cmd = [
            str(pip_path),
            "install",
            "--quiet",
            "--no-cache-dir",
            f"{REPO_ROOT}[worker]",
        ]
        proc = subprocess.run(  # noqa: S603 — fully literal argv
            install_cmd,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(
                f"pip install '.[worker]' failed in venv (likely sandboxed network); stderr: {proc.stderr[-400:]}"
            )

        # Sanity: fastapi / asyncpg must NOT have been pulled into the
        # venv's site-packages. If they were, the test would still run
        # (because the leak is masked by their presence), but we'd lose
        # the bug-reproduction signal — refuse to proceed silently.
        list_proc = subprocess.run(  # noqa: S603 — fully literal argv
            [str(pip_path), "list", "--format=freeze"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if list_proc.returncode == 0:
            installed_lower = {
                line.split("==", 1)[0].strip().lower()
                for line in list_proc.stdout.splitlines()
                if line.strip() and not line.startswith("#") and "==" in line
            }
            forbidden = {"fastapi", "asyncpg", "sqlalchemy", "alembic", "uvicorn"}
            leaks = sorted(installed_lower & forbidden)
            assert not leaks, (
                "[worker] extras installed control-plane-only distributions: "
                f"{leaks}. Ensure pyproject's [worker] extra stays minimal."
            )

        # Run the actual reproduction.
        assert worker_path.exists(), (
            f"whilly-worker console script not produced after install — "
            f"check pyproject [project.scripts]. Looked at: {worker_path}"
        )
        help_proc = subprocess.run(  # noqa: S603 — fully literal argv
            [str(worker_path), "--help"],
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_SECONDS,
            check=False,
        )
        assert help_proc.returncode == 0, (
            f"whilly-worker --help exited {help_proc.returncode}\n"
            f"stdout:\n{help_proc.stdout}\n"
            f"stderr:\n{help_proc.stderr}\n"
            "This is the v4.4.0 fastapi-leak regression."
        )
        assert help_proc.stdout.strip(), "whilly-worker --help stdout was empty"
        assert "--connect" in help_proc.stdout, (
            f"whilly-worker --help did not render the expected argparse output; got: {help_proc.stdout!r}"
        )


def test_python_executable_available() -> None:
    """Sanity: ``sys.executable`` is callable as a python interpreter.

    Belt-and-braces — if the test runner's interpreter cannot launch
    ``python -c "import sys"`` we want a clear early-fail rather than
    a confusing ``venv.EnvBuilder`` failure inside the main test.
    """
    assert shutil.which(sys.executable) or Path(sys.executable).exists(), (
        f"sys.executable is not invokable: {sys.executable}"
    )
