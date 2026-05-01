"""M1 — docker-compose.worker.yml interpolation gate (round-2 fix).

Background
----------
Round-1 scrutiny on m1-compose-worker (synthesis line 32-37) flagged
that ``docker-compose -f docker-compose.worker.yml config -q`` failed
on a clean checkout because two environment variables —
``WHILLY_CONTROL_URL`` and ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` — used the
``${VAR:?error}`` (required) interpolation form. Compose evaluates
that during ``config`` (not just at ``up`` time), so an operator
running ``config -q`` to lint the file before booting a stack got a
hard error even though the file is syntactically valid.

VAL-M1-COMPOSE-003 explicitly requires that exact command exit 0 in
a clean checkout. The fix flipped the two interpolations to the
``:-`` (default-if-unset) form with an empty default, and we now
rely on ``docker/entrypoint.sh``'s existing ``: "${VAR:?...}"``
guards to fail loudly at ``up`` time if either variable is still
empty. This module pins both halves of that contract:

1. ``config -q`` succeeds on a CLEAN env (no env file, no exported
   vars). [VAL-M1-COMPOSE-003]
2. ``config -q`` succeeds with explicit values supplied via env vars.
3. ``config -q`` succeeds against the committed ``.env.worker.example``.
4. ``--services`` lists exactly ``worker`` (no ``postgres``,
   ``control-plane``, or other M1 add-ons leaked in).
   [VAL-M1-COMPOSE-003 — services list]
5. The compose YAML no longer contains any ``:?`` (required)
   interpolations on those two variables — guards against regression.
6. ``docker/entrypoint.sh`` still has the runtime ``:?`` guards that
   make the worker container exit non-zero with a clear stderr message
   when the variables are unset/empty at boot — guards against the
   silent-pass alternative implementation.

The tests are structured as integration-tier even though they don't
need the Docker daemon — only the ``docker-compose`` CLI binary
itself. Compose's ``config`` subcommand parses YAML and runs env
interpolation purely client-side. We skip cleanly when the binary is
absent so this module doesn't gate developer machines without Docker
installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.worker.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.worker.example"
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"

# Names of the env vars whose interpolation form is the subject of this gate.
GATED_VARS = ("WHILLY_CONTROL_URL", "WHILLY_WORKER_BOOTSTRAP_TOKEN")


_DOCKER_COMPOSE = pytest.mark.skipif(
    shutil.which("docker-compose") is None,
    reason="docker-compose CLI not available; M1 compose validation requires v2.40+",
)


def _clean_env() -> dict[str, str]:
    """Return the current environment with the gated vars stripped.

    The mission's M1 compose contract requires `docker-compose config -q`
    to succeed even when *no* environment is set — i.e. simulating an
    operator running the command in a fresh checkout before they have
    written ``.env.worker``. We can't ``unset`` from the test process
    safely (subprocess inherits its parent's env), so we strip the
    gated vars from the env dict we hand to the subprocess instead.
    """
    env = dict(os.environ)
    for name in GATED_VARS:
        env.pop(name, None)
    return env


def _run_compose_config(extra_args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `docker-compose -f docker-compose.worker.yml <extra_args>`.

    Returns the completed process; callers assert on returncode/stdout/stderr.
    Always runs from REPO_ROOT so the relative ``./workspace`` volume in the
    compose file resolves identically to how an operator runs it.
    """
    cmd = ["docker-compose", "-f", str(COMPOSE_FILE), *extra_args]
    return subprocess.run(  # noqa: S603 — args list is fully literal
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ─── Core contract: VAL-M1-COMPOSE-003 ───────────────────────────────────


@_DOCKER_COMPOSE
def test_config_q_succeeds_with_no_env() -> None:
    """`config -q` on a clean checkout (no env vars set) must exit 0.

    This is the literal text of VAL-M1-COMPOSE-003. Round-1 scrutiny
    found that `:?` interpolation on WHILLY_CONTROL_URL /
    WHILLY_WORKER_BOOTSTRAP_TOKEN broke this. Round-2 fix flipped them
    to `:-` defaults; this test pins the fix.
    """
    result = _run_compose_config(["config", "-q"], env=_clean_env())
    assert result.returncode == 0, (
        f"docker-compose config -q failed (exit {result.returncode}). "
        f"This is the round-1 scrutiny regression — "
        f"WHILLY_CONTROL_URL / WHILLY_WORKER_BOOTSTRAP_TOKEN must NOT use "
        f"`:?` (required) interpolation; use `:-` (default-if-unset) and "
        f"rely on docker/entrypoint.sh's runtime check.\nstderr:\n{result.stderr}"
    )


@_DOCKER_COMPOSE
def test_config_q_succeeds_with_explicit_env() -> None:
    """Happy path: `config -q` with both variables supplied still parses."""
    env = _clean_env()
    env["WHILLY_CONTROL_URL"] = "http://127.0.0.1:8000"
    env["WHILLY_WORKER_BOOTSTRAP_TOKEN"] = "demo-bootstrap"
    result = _run_compose_config(["config", "-q"], env=env)
    assert result.returncode == 0, f"explicit-env config -q failed:\n{result.stderr}"


@_DOCKER_COMPOSE
def test_config_q_succeeds_with_env_file_example() -> None:
    """`--env-file .env.worker.example` parses cleanly.

    Operators copy the example to .env.worker before booting; the
    example file itself MUST always be a valid input for `config -q`
    (services.yaml registers this exact command as
    ``m1_worker_config_example``).
    """
    assert ENV_EXAMPLE.exists(), f"missing .env.worker.example at {ENV_EXAMPLE}"
    result = _run_compose_config(
        ["--env-file", str(ENV_EXAMPLE), "config", "-q"],
        env=_clean_env(),
    )
    assert result.returncode == 0, f"--env-file config -q failed:\n{result.stderr}"


@_DOCKER_COMPOSE
def test_config_services_lists_only_worker() -> None:
    """`config --services` lists exactly the `worker` service.

    Per VAL-M1-COMPOSE-003: no `postgres`, no `control-plane`, no
    seed/migrate add-ons leaked in.
    """
    result = _run_compose_config(["config", "--services"], env=_clean_env())
    assert result.returncode == 0, f"config --services failed:\n{result.stderr}"
    services = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
    assert services == ["worker"], f"docker-compose.worker.yml must declare ONLY the worker service; got {services!r}"


# ─── Regression guards (compose-spec parse check, no daemon needed) ──────


def _strip_yaml_comments(text: str) -> str:
    """Drop YAML comments (lines whose first non-whitespace char is `#`).

    We want the regression check to fire on actual interpolation in
    *value position* — not on the explanatory commentary in the file's
    header that legitimately mentions `${VAR:?...}` to remind future
    editors why we DON'T use that form. Inline `# ...` after a value is
    not handled here because none of the gated lines carry an inline
    comment, and a naive split would corrupt strings.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_compose_file_uses_default_form_for_gated_vars() -> None:
    """The two gated env vars must NOT use `:?` (required) interpolation.

    YAML/text-level guard. Runs in unit tier (no docker-compose binary
    needed). If a future change tries to re-introduce a `:?` form on
    these names, the test fails with a pointer to round-1 scrutiny.

    Only non-comment lines are inspected — the comment block above
    each gated var deliberately mentions ``${VAR:?...}`` to explain
    why we no longer use that form, and we don't want THAT to trip
    the regression check.
    """
    code = _strip_yaml_comments(COMPOSE_FILE.read_text(encoding="utf-8"))
    for name in GATED_VARS:
        # Reject the strict-required form `${NAME:?...}` in any value position.
        bad_token = f"${{{name}:?"
        assert bad_token not in code, (
            f"{COMPOSE_FILE.name} reintroduces the `:?` (required) "
            f"interpolation on {name}. That form makes "
            f"`docker-compose config -q` fail on a clean checkout — see "
            f"VAL-M1-COMPOSE-003 / round-1 scrutiny line 32-37. Use "
            f"`${{{name}:-}}` instead and rely on docker/entrypoint.sh "
            f"to fail loudly at runtime."
        )
        # Confirm the default-if-unset form IS present in a value position.
        good_token = f"${{{name}:-"
        assert good_token in code, (
            f"{COMPOSE_FILE.name} no longer interpolates {name} with the "
            f"`:-` default-if-unset form; this gate expects "
            f"`${{{name}:-...}}` to be present in a value position."
        )


def test_entrypoint_still_validates_gated_vars_at_runtime() -> None:
    """`docker/entrypoint.sh` keeps the runtime `:?` guards.

    The compose-file fix relies on the entrypoint to fail loudly when
    these vars are still unset/empty at `docker-compose up` time. If a
    future refactor removes the entrypoint guard, the worker would
    silently boot with empty values — this test prevents that drift.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for name in GATED_VARS:
        # The entrypoint uses a `: "${NAME:?msg}"` shell idiom — i.e.
        # the colon-no-op operator followed by `${NAME:?...}` so the
        # shell errors out (with `msg` on stderr) when NAME is unset
        # OR empty. Any occurrence of `${NAME:?` is sufficient evidence
        # that the runtime guard is in place.
        token = f"${{{name}:?"
        assert token in text, (
            f"docker/entrypoint.sh no longer has a `${{{name}:?...}}` "
            f"runtime guard. The compose-file fix for round-1 scrutiny "
            f"depends on this guard to fail-loud at `up` time. Restore "
            f"the entrypoint check before relaxing the compose-file "
            f"interpolation form."
        )
