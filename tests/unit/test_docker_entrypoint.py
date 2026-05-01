"""Unit tests for ``docker/entrypoint.sh`` (M1 — m1-entrypoint-switch).

The entrypoint is the boundary between the published Docker image and the
``whilly`` / ``whilly-worker`` Python entry points. It is shell, not Python,
but its behavioural contract is the same scope as any other CLI surface:

* role dispatch (``control-plane`` / ``worker`` / ``migrate`` / ``shell``)
* env-var validation (fail-fast on missing required inputs)
* opt-in feature flag (``WHILLY_USE_CONNECT_FLOW``) with explicit truthiness
* exit-code propagation (no ``|| true`` swallowing of child failures)

We exercise these by invoking the script in a controlled ``PATH``-shimmed
environment that replaces the production tools (``whilly``, ``whilly-worker``,
``curl``, ``alembic``, ``python``) with shell stubs that record their argv to
a captured file. The shim approach means we never need a real Postgres or a
running control plane to assert "the entrypoint dispatched the right command
with the right flags".

The tests pin the fulfils for VAL-M1-ENTRYPOINT-* assertions:

* VAL-M1-ENTRYPOINT-001 — default behaviour unchanged (legacy register-then-exec)
* VAL-M1-ENTRYPOINT-002 — ``WHILLY_USE_CONNECT_FLOW=1`` switches to ``whilly worker connect``
* VAL-M1-ENTRYPOINT-005 — fail-fast diagnostics on missing required env
* VAL-M1-ENTRYPOINT-006 — ``WHILLY_INSECURE`` forwarded as ``--insecure``
* VAL-M1-ENTRYPOINT-901 — full truthiness matrix for ``WHILLY_USE_CONNECT_FLOW``
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
    """Yield a directory of executable shell shims and a captured-argv log path.

    Each shim writes a single ``"<name>: <args>"`` line to ``argv.log`` and
    exits 0. Tests prepend this directory to ``PATH`` so the real binaries
    (``whilly``, ``whilly-worker``, ``curl``, ``alembic``, ``python``,
    ``hostname``) are never invoked. The capture file is named ``argv.log``
    inside the shim dir so tests can read it directly.
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    log_path = shim_dir / "argv.log"

    # The ``whilly`` shim is special: the legacy register branch in the
    # entrypoint pipes ``whilly worker register ...`` stdout into ``awk``
    # to extract ``worker_id:`` and ``token:``. If the shim returns
    # nothing, the entrypoint's own "failed to parse register output"
    # diagnostic fires and the test sees exit 2 — masking the real bug.
    # So we emit deterministic placeholder values on stdout when the
    # subcommand is ``register``, and a single argv-log line otherwise.
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

    # Other binaries are pure argv recorders.
    for name in ("whilly-worker", "curl", "alembic", "python"):
        shim = shim_dir / name
        shim.write_text(
            f'#!/usr/bin/env bash\nprintf "%s: %s\\n" "{name}" "$*" >> "{log_path}"\nexit 0\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    # `hostname` is invoked by the connect-flow branch; pin it.
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
    """Invoke ``entrypoint.sh`` with ``PATH`` pointed at ``shim_dir``.

    ``env`` overrides default environment variables; the system's ``PATH``
    is preserved (so ``bash``, ``awk``, ``tr``, ``date`` keep working) but
    prefixed with ``shim_dir`` so the shimmed binaries win lookup.
    """
    base_env = {
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(shim_dir.parent),
        # Many shells re-derive these from PWD; setting a fixed CWD avoids
        # accidentally running scripts found in the operator's $HOME.
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


# ---------------------------------------------------------------------------
# Truthiness matrix — VAL-M1-ENTRYPOINT-901
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "On"])
def test_use_connect_flow_truthy_values_take_connect_path(value: str, shim_bin: Path) -> None:
    """1 / true / yes / on (case-insensitive) all enable the connect flow.

    The shim records the argv that the entrypoint exec'd into. With the
    flag truthy, the entrypoint must hand off to ``whilly worker connect``
    (NOT ``whilly worker register`` from the legacy bash-awk path).
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": value,
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    # The connect branch records "whilly: worker connect ..." via the shim.
    assert "whilly: worker connect" in captured, captured
    # And it must NOT have invoked the legacy register subcommand.
    assert "whilly: worker register" not in captured, captured
    # And it must have noted the new code path on stderr (VAL-M1-ENTRYPOINT-002).
    assert "using connect flow" in result.stderr


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", "  ", "anything-else"])
def test_use_connect_flow_falsy_values_take_legacy_path(value: str, shim_bin: Path) -> None:
    """0 / false / no / off / empty / unrecognised values keep the legacy path.

    The legacy path is what the v4.3.1 image shipped: register via bootstrap
    when no per-worker token is set, then exec ``whilly-worker``.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": value,
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    # Legacy path uses `whilly worker register` AND then `whilly-worker`.
    assert "whilly: worker register" in captured, captured
    assert "whilly worker connect" not in result.stderr  # no connect log line
    # And it must NOT have logged the new code path (preserves v4.3.1 stderr shape).
    assert "using connect flow" not in result.stderr


def test_use_connect_flow_unset_takes_legacy_path(shim_bin: Path) -> None:
    """Unset env var (not just empty string) defaults to legacy behaviour.

    This matches VAL-M1-ENTRYPOINT-001's "byte-equivalent stderr/stdout up
    to TS" — when nothing in the env touches the flag, the entrypoint must
    behave exactly as v4.3.1.
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
    assert "whilly: worker register" in captured, captured
    assert "using connect flow" not in result.stderr


# ---------------------------------------------------------------------------
# Connect-flow argv shape — VAL-M1-ENTRYPOINT-002 / -006
# ---------------------------------------------------------------------------


def test_connect_flow_forwards_required_args(shim_bin: Path) -> None:
    """The connect flow must pass URL, bootstrap-token, plan, hostname through.

    These four are the minimum viable args for ``whilly worker connect``;
    forgetting any of them means the operator gets an opaque CLI error
    rather than the entrypoint's own diagnostic.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo-m1",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "worker connect http://127.0.0.1:8000" in captured
    assert "--bootstrap-token boot-secret" in captured
    assert "--plan demo-m1" in captured
    assert "--hostname test-host" in captured


def test_connect_flow_does_not_pass_insecure_by_default(shim_bin: Path) -> None:
    """Without ``WHILLY_INSECURE``, ``--insecure`` must not leak into argv.

    The contract is opt-in; default-on would silently weaken the scheme
    guard for every operator who happens to run the new flow.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "--insecure" not in captured, captured


@pytest.mark.parametrize("insecure_value", ["1", "true", "yes", "on"])
def test_connect_flow_forwards_insecure_when_truthy(insecure_value: str, shim_bin: Path) -> None:
    """``WHILLY_INSECURE`` truthy → ``--insecure`` appended to argv.

    This is the entrypoint half of VAL-M1-ENTRYPOINT-006; the URL-scheme
    enforcement itself is owned by ``whilly worker connect``.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_INSECURE": insecure_value,
            "WHILLY_CONTROL_URL": "http://192.0.2.10:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "--insecure" in captured, captured


@pytest.mark.parametrize("insecure_value", ["0", "false", "no", "off", ""])
def test_connect_flow_omits_insecure_when_falsy(insecure_value: str, shim_bin: Path) -> None:
    """``WHILLY_INSECURE`` falsy → no ``--insecure`` flag passed."""
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_INSECURE": insecure_value,
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "--insecure" not in captured, captured


# ---------------------------------------------------------------------------
# Worker-runtime arg pass-through via ``--`` sentinel
# (fix-m1-entrypoint-switch-arg-passthrough)
# ---------------------------------------------------------------------------


def test_connect_flow_emits_double_dash_separator_with_no_extra_args(shim_bin: Path) -> None:
    """No extra ``"$@"`` args → entrypoint still emits the bare ``--`` separator.

    The connect parser tolerates a trailing bare ``--`` (passthrough is
    empty), so this is a noop on the worker side. We pin it here to lock
    the entrypoint shape in: future audits won't accidentally remove the
    sentinel and reintroduce the original "argparse rejects --once" bug.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    # The shim records the full argv on a single space-joined line. We
    # expect ``... --hostname test-host --`` with the sentinel as the
    # final token (whitespace-trimmed).
    connect_lines = [line for line in captured.splitlines() if "worker connect" in line]
    assert connect_lines, captured
    assert connect_lines[0].rstrip().endswith("--"), connect_lines[0]


def test_connect_flow_passes_once_through_double_dash(shim_bin: Path) -> None:
    """``entrypoint.sh worker --once`` reaches connect's argv after ``--``.

    Repro guard for the original Scrutiny round-1 finding: with the
    flag enabled, ``--once`` must NOT be parsed by ``whilly worker
    connect``'s argparse (which would reject it). Instead it lands
    after the ``--`` sentinel for the exec'd ``whilly-worker``.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker", "--once"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    # `--once` must appear AFTER the `--` separator in the recorded argv.
    connect_lines = [line for line in captured.splitlines() if "worker connect" in line]
    assert connect_lines, captured
    line = connect_lines[0]
    sep = " -- "
    assert sep in line, line
    after = line.split(sep, 1)[1]
    assert "--once" in after, after


def test_connect_flow_passes_mixed_worker_runtime_args(shim_bin: Path) -> None:
    """Multiple worker-runtime flags after ``worker`` all land after ``--``.

    Covers the realistic operator workflow of overriding worker
    identity + heartbeat cadence + a bounded run via env+CLI mix:
    ``docker run ... worker --worker-id w-1 --heartbeat-interval 5
    --max-iterations 3``.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        [
            "worker",
            "--worker-id",
            "w-custom",
            "--heartbeat-interval",
            "5",
            "--max-iterations",
            "3",
        ],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    connect_lines = [line for line in captured.splitlines() if "worker connect" in line]
    assert connect_lines, captured
    line = connect_lines[0]
    after = line.split(" -- ", 1)[1] if " -- " in line else ""
    assert "--worker-id w-custom" in after, after
    assert "--heartbeat-interval 5" in after, after
    assert "--max-iterations 3" in after, after
    # Connect-side args (--bootstrap-token, --plan, --hostname) stay
    # BEFORE the separator — they belong to connect's argparse.
    before = line.split(" -- ", 1)[0]
    assert "--bootstrap-token boot-secret" in before, before
    assert "--plan demo" in before, before
    assert "--hostname test-host" in before, before


def test_connect_flow_pure_connect_args_do_not_leak_to_passthrough(shim_bin: Path) -> None:
    """Pure connect-CLI args (env-driven) live BEFORE ``--``, never after.

    ``--insecure`` is a connect-side flag (it controls the URL scheme
    guard); the entrypoint must keep emitting it as part of the
    connect argv, not as part of the post-sentinel passthrough.
    """
    log_path = shim_bin / "argv.log"
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_INSECURE": "1",
            "WHILLY_CONTROL_URL": "http://192.0.2.10:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    captured = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    connect_lines = [line for line in captured.splitlines() if "worker connect" in line]
    assert connect_lines, captured
    line = connect_lines[0]
    if " -- " in line:
        before, after = line.split(" -- ", 1)
    else:
        before, after = line, ""
    assert "--insecure" in before, before
    assert "--insecure" not in after, after


# ---------------------------------------------------------------------------
# Exit-code propagation — VAL-M1-ENTRYPOINT-005 / -902
# ---------------------------------------------------------------------------


def test_connect_flow_propagates_failure_exit_code(shim_bin: Path) -> None:
    """A failing ``whilly worker connect`` exits the entrypoint non-zero.

    VAL-M1-ENTRYPOINT-902 specifically: with connect-flow enabled and the
    underlying binary broken (here we replace the shim with a non-zero
    exiter), the container exits non-zero. The entrypoint must NOT swallow
    the failure with ``|| true`` or similar.
    """
    # Replace the success-shim with one that exits 17 — a value distinct
    # from any standard exit code so we can pin propagation precisely.
    failing = shim_bin / "whilly"
    failing.write_text(
        '#!/usr/bin/env bash\nprintf "boom\\n" >&2\nexit 17\n',
        encoding="utf-8",
    )
    failing.chmod(0o755)

    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 17, (result.returncode, result.stderr)


def test_connect_flow_missing_control_url_exits_nonzero(shim_bin: Path) -> None:
    """VAL-M1-ENTRYPOINT-005: missing ``WHILLY_CONTROL_URL`` fails fast.

    The diagnostic must mention the variable name so the operator can fix
    the misconfiguration without reading source.
    """
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_PLAN_ID": "demo",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode != 0
    assert "WHILLY_CONTROL_URL" in result.stderr


def test_connect_flow_missing_bootstrap_token_exits_nonzero(shim_bin: Path) -> None:
    """Connect flow needs the bootstrap secret to register; missing → fail.

    Bootstrap-token is the cluster-join secret consumed by ``whilly worker
    connect``; without it there is no authentication for register.
    """
    result = _run_entrypoint(
        ["worker"],
        env={
            "WHILLY_USE_CONNECT_FLOW": "1",
            "WHILLY_CONTROL_URL": "http://127.0.0.1:8000",
            "WHILLY_PLAN_ID": "demo",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode != 0
    assert "WHILLY_WORKER_BOOTSTRAP_TOKEN" in result.stderr


# ---------------------------------------------------------------------------
# Control-plane gate change — feature scope
# ---------------------------------------------------------------------------


def test_control_plane_no_longer_requires_worker_token(shim_bin: Path) -> None:
    """control-plane role must NOT abort when ``WHILLY_WORKER_TOKEN`` is unset.

    Pre-feature: the entrypoint had ``: "${WHILLY_WORKER_TOKEN:?...}"`` at the
    top of the control-plane branch, forcing m1-compose-control-plane to
    default the variable to a placeholder. Post-feature: only the bootstrap
    token + DB URL are mandatory. Bearer auth is optional on the server
    side, so requiring it on the client side was a footgun.

    We assert the control-plane branch reaches its ``alembic upgrade head``
    log line — that's well past the env-var gates and proves the gate has
    moved out of the way.
    """
    result = _run_entrypoint(
        ["control-plane"],
        env={
            "WHILLY_DATABASE_URL": "postgresql://x@127.0.0.1/x",
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode == 0, result.stderr
    assert "applying alembic migrations" in result.stderr


def test_control_plane_still_requires_database_url(shim_bin: Path) -> None:
    """Control-plane still aborts on missing ``WHILLY_DATABASE_URL``.

    Sanity check that we only relaxed the worker-token gate, not all of them.
    Without the database URL the control-plane has nothing to talk to.
    """
    result = _run_entrypoint(
        ["control-plane"],
        env={
            "WHILLY_WORKER_BOOTSTRAP_TOKEN": "boot-secret",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode != 0
    assert "WHILLY_DATABASE_URL" in result.stderr


def test_control_plane_still_requires_bootstrap_token(shim_bin: Path) -> None:
    """Control-plane needs the cluster-join secret — that gate stays.

    Without the bootstrap token, ``whilly worker connect`` and the legacy
    bash register path on workers can never authenticate.
    """
    result = _run_entrypoint(
        ["control-plane"],
        env={
            "WHILLY_DATABASE_URL": "postgresql://x@127.0.0.1/x",
        },
        shim_dir=shim_bin,
    )
    assert result.returncode != 0
    assert "WHILLY_WORKER_BOOTSTRAP_TOKEN" in result.stderr


# ---------------------------------------------------------------------------
# Static — shellcheck stays clean
# ---------------------------------------------------------------------------


def test_entrypoint_passes_shellcheck() -> None:
    """``shellcheck docker/entrypoint.sh`` must report zero diagnostics.

    The CI image installs shellcheck; locally the test is skipped if the
    binary isn't on PATH so the suite still runs on a fresh dev box.
    """
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
