"""Unit tests for ``docker/entrypoint.sh`` legacy register-and-exec path.

Companion to ``tests/unit/test_docker_entrypoint.py``. Where that file
exercises the connect-flow branch (``WHILLY_USE_CONNECT_FLOW=1``) and the
top-level role gates, this file pins the *legacy* worker path — the one
that runs by default and that v4.3.1 operators upgraded from. Specifically
it asserts the post-register ``exec whilly-worker ...`` argv shape with
respect to ``WHILLY_INSECURE``:

* VAL-M1-ENTRYPOINT-001 (m1-round5-contract-rereframer): ``WHILLY_INSECURE=1``
  on the legacy path opts in and restores v4.3.1 behavior of allowing plain
  HTTP to non-loopback control-plane URLs. Without it, the worker's URL
  scheme guard rejects the connection — that's the fail-secure default.

The shim approach mirrors the existing entrypoint test: replace
``whilly`` / ``whilly-worker`` / ``curl`` / ``hostname`` with shell stubs
that record their argv to ``argv.log`` and exit 0. We then read the log to
confirm whether ``--insecure`` made it onto the post-register
``whilly-worker`` invocation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="entrypoint is bash-only; Windows / no-bash environments are out of scope",
)


@pytest.fixture
def shim_bin(tmp_path: Path) -> Iterator[Path]:
    """Yield a directory of executable shell shims and a captured-argv log path."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    log_path = shim_dir / "argv.log"

    whilly_shim = shim_dir / "whilly"
    whilly_shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "whilly: %s\\n" "$*" >> "{log_path}"\n'
        'if [[ "${1:-}" == "worker" && "${2:-}" == "register" ]]; then\n'
        '  printf "worker_id: w-test\\ntoken: bearer-test\\n"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    whilly_shim.chmod(0o755)

    for name in ("whilly-worker", "curl", "alembic", "python"):
        shim = shim_dir / name
        shim.write_text(
            f'#!/usr/bin/env bash\nprintf "%s: %s\\n" "{name}" "$*" >> "{log_path}"\nexit 0\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    hostname_shim = shim_dir / "hostname"
    hostname_shim.write_text(
        "#!/usr/bin/env bash\nprintf 'test-host\\n'\n",
        encoding="utf-8",
    )
    hostname_shim.chmod(0o755)
    yield shim_dir


def _run_entrypoint(
    args: list[str],
    *,
    env: dict[str, str],
    shim_dir: Path,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    base_env = {
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(shim_dir.parent),
        "PWD": str(shim_dir.parent),
    }
    base_env.update(env)
    return subprocess.run(
        ["bash", str(ENTRYPOINT), *args],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=str(shim_dir.parent),
    )


def _worker_argv_lines(captured: str) -> list[str]:
    """Filter shim log to lines that recorded the post-register ``whilly-worker`` argv."""
    return [line for line in captured.splitlines() if line.startswith("whilly-worker:")]


# ---------------------------------------------------------------------------
# Baseline — no WHILLY_INSECURE
# ---------------------------------------------------------------------------


def test_legacy_path_without_insecure_does_not_pass_flag(shim_bin: Path) -> None:
    """Default legacy path (no ``WHILLY_INSECURE``) must not append ``--insecure``.

    This is the fail-secure default: the worker's URL scheme guard rejects
    plain HTTP to non-loopback URLs unless explicitly opted out of. The
    entrypoint mirrors that contract — only forward the flag when the
    operator asked for it.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" not in worker_lines[0], worker_lines[0]
    assert "--connect http://127.0.0.1:8000" in worker_lines[0]
    assert "--token bearer-test" in worker_lines[0]
    assert "--plan demo" in worker_lines[0]


def test_legacy_path_unset_insecure_does_not_pass_flag(shim_bin: Path) -> None:
    """``WHILLY_INSECURE`` unset (not even empty) → no ``--insecure`` on argv.

    Distinct from the empty-string case below because bash ``${var:-}``
    treats unset and empty identically; we still pin both to lock the
    contract from regressing.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" not in worker_lines[0], worker_lines[0]


# ---------------------------------------------------------------------------
# Truthy — WHILLY_INSECURE=1/true/yes/on (case-insensitive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "On"])
def test_legacy_path_truthy_insecure_appends_flag(value: str, shim_bin: Path) -> None:
    """Truthy ``WHILLY_INSECURE`` (case-insensitive) → ``--insecure`` on worker argv.

    Mirrors the connect-flow branch's behaviour at lines 145-147 of the
    entrypoint and reuses the same ``is_truthy`` helper. The
    contract reframer (m1-round5-contract-rereframer) made
    ``WHILLY_INSECURE=1`` the explicit opt-in for v4.3.1-style plain-HTTP
    non-loopback control-plane reach on the legacy register path; this
    test pins that opt-in.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_INSECURE": value,
            "WHILLY_CONTROL_URL": "http://192.0.2.10:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" in worker_lines[0], worker_lines[0]


# ---------------------------------------------------------------------------
# Falsy — WHILLY_INSECURE=0/false/no/off/empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "NO", "off", "OFF", "", "anything-else"])
def test_legacy_path_falsy_insecure_omits_flag(value: str, shim_bin: Path) -> None:
    """Falsy / unrecognised ``WHILLY_INSECURE`` → no ``--insecure`` on worker argv.

    The ``is_truthy`` helper recognises only ``1 / true / yes / on``
    (case-insensitive). Any other value — including the empty string and
    the literal "anything-else" — keeps the secure default.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_INSECURE": value,
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" not in worker_lines[0], worker_lines[0]


# ---------------------------------------------------------------------------
# Pre-supplied WHILLY_WORKER_TOKEN (skip-register sub-branch of legacy path)
# ---------------------------------------------------------------------------


def test_legacy_path_with_pre_supplied_token_and_truthy_insecure_appends_flag(shim_bin: Path) -> None:
    """``WHILLY_INSECURE=1`` works on the no-register sub-branch too.

    When ``WHILLY_WORKER_TOKEN`` is already in env, the entrypoint skips
    the bootstrap register call and execs ``whilly-worker`` directly. The
    ``--insecure`` opt-in must apply to this sub-branch identically — both
    sub-branches share the same ``exec`` block.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_INSECURE": "1",
            "WHILLY_CONTROL_URL": "http://192.0.2.10:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_TOKEN": "pre-supplied-bearer",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" in worker_lines[0], worker_lines[0]
    assert "--token pre-supplied-bearer" in worker_lines[0]
    assert "whilly: worker register" not in captured, captured


def test_legacy_path_with_pre_supplied_token_and_no_insecure_omits_flag(shim_bin: Path) -> None:
    """No ``WHILLY_INSECURE`` on the skip-register sub-branch → no ``--insecure``."""
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_TOKEN": "pre-supplied-bearer",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" not in worker_lines[0], worker_lines[0]


# ---------------------------------------------------------------------------
# Positional pass-through ("$@") still reaches the worker
# ---------------------------------------------------------------------------


def test_legacy_path_appends_positional_args_after_insecure(shim_bin: Path) -> None:
    """Positional ``"$@"`` extras (e.g. ``--once``) still land on worker argv.

    The fix wraps the exec into an array (``worker_argv``) and appends
    ``"$@"`` after the optional ``--insecure``. This test pins that any
    operator-supplied trailing flags (like ``--once``) make it through
    to ``whilly-worker`` regardless of whether ``--insecure`` is set.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker", "--once"],
        env={
            "WHILLY_INSECURE": "1",
            "WHILLY_CONTROL_URL": "http://192.0.2.10:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    worker_lines = _worker_argv_lines(captured)
    assert worker_lines, captured
    assert "--insecure" in worker_lines[0], worker_lines[0]
    assert "--once" in worker_lines[0], worker_lines[0]
