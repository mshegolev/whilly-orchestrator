"""Integration test: docker-compose.demo.yml worker service exposes
the v4.4 opencode + Groq defaults (m1-opencode-groq-default).

Pins three behavioural invariants on the canonical demo compose file:

1. ``services.worker.environment.WHILLY_CLI`` defaults to ``opencode``.
2. ``services.worker.environment.WHILLY_MODEL`` defaults to
   ``groq/openai/gpt-oss-120b``.
3. ``services.worker.environment`` references ``GROQ_API_KEY`` (so the
   host's ``.env`` value, if any, is forwarded into the container).

Each value uses Compose's ``${VAR:-default}`` expansion so operators can
override on the command line / via ``.env``.

Skips cleanly when the Docker daemon isn't reachable for the optional
``docker-compose config -q`` syntactic-validity check; the YAML parse +
default-value assertions always run because they don't need Docker.

Backs VAL-M1-AGENT-DEFAULT-001.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
COMPOSE_FILE: Path = REPO_ROOT / "docker-compose.demo.yml"


def _load_worker_environment() -> dict[str, str]:
    """Parse compose YAML and return the worker service's environment dict.

    Mirrors the helper in ``tests/unit/test_demo_compose_has_insecure_for_worker.py``
    — Compose ``environment:`` may be a mapping or a list of ``KEY=VAL``
    strings. We normalise to ``dict[str, str]`` so callers don't care.
    """
    assert COMPOSE_FILE.is_file(), f"missing {COMPOSE_FILE}"
    raw = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "compose file must parse as a mapping"
    services = raw.get("services") or {}
    worker = services.get("worker")
    assert worker is not None, "compose file is missing the 'worker' service"
    env = worker.get("environment")
    assert env is not None, "worker service is missing 'environment:' block"

    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    if isinstance(env, list):
        out: dict[str, str] = {}
        for item in env:
            assert isinstance(item, str), f"unexpected env entry: {item!r}"
            if "=" in item:
                k, v = item.split("=", 1)
                out[k] = v
            else:
                out[item] = ""
        return out
    raise AssertionError(f"worker.environment has unsupported type: {type(env).__name__}")


def _docker_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the Docker daemon."""
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
        return False, f"`docker info` exited {proc.returncode}"
    return True, "ok"


# ──────────────────────────────────────────────────────────────────────────────
# YAML-only assertions — always run, no Docker needed
# ──────────────────────────────────────────────────────────────────────────────


def test_worker_environment_has_whilly_cli_opencode_default() -> None:
    """``WHILLY_CLI`` must default to ``opencode`` via ``${WHILLY_CLI:-opencode}``."""
    env = _load_worker_environment()
    assert "WHILLY_CLI" in env, "worker.environment must declare WHILLY_CLI"
    raw = env["WHILLY_CLI"]
    # The literal string accepted by compose's expansion is ``${WHILLY_CLI:-opencode}``.
    # Allow a plain ``opencode`` value too in case a future PR drops the override.
    assert "opencode" in raw, (
        f"worker.environment.WHILLY_CLI must default to 'opencode' (m1-opencode-groq-default); got: {raw!r}"
    )
    # Defensive: catch the legacy empty default that breaks the new behaviour.
    assert raw not in {"${WHILLY_CLI:-}", ""}, (
        f"WHILLY_CLI default was reverted to the empty string ({raw!r}). "
        "v4.4 mandates the worker default to 'opencode'."
    )


def test_worker_environment_has_whilly_model_groq_default() -> None:
    """``WHILLY_MODEL`` must default to ``groq/openai/gpt-oss-120b``."""
    env = _load_worker_environment()
    assert "WHILLY_MODEL" in env, "worker.environment must declare WHILLY_MODEL"
    raw = env["WHILLY_MODEL"]
    assert "groq/openai/gpt-oss-120b" in raw, (
        f"worker.environment.WHILLY_MODEL must default to 'groq/openai/gpt-oss-120b' "
        f"(m1-opencode-groq-default); got: {raw!r}"
    )


def test_worker_environment_forwards_groq_api_key() -> None:
    """``GROQ_API_KEY`` must be forwarded from the host env / ``.env`` to the worker.

    The expected literal is ``${GROQ_API_KEY:-}`` (empty default — never
    a real secret committed to YAML). Operators put the actual key into
    the gitignored ``.env`` file at the repo root.
    """
    env = _load_worker_environment()
    assert "GROQ_API_KEY" in env, (
        "worker.environment must reference GROQ_API_KEY so the host's "
        ".env value is forwarded into the worker container."
    )
    raw = env["GROQ_API_KEY"]
    # Either ``${GROQ_API_KEY:-}`` (with empty default) or ``${GROQ_API_KEY}``
    # (mandatory) is acceptable — both forward the host env.
    assert "GROQ_API_KEY" in raw, (
        f"worker.environment.GROQ_API_KEY must be a Compose substitution "
        f"(got: {raw!r}). Bare literal values are forbidden — they would "
        "imply a committed secret."
    )


def test_no_real_groq_key_committed() -> None:
    """Defence-in-depth: confirm no high-entropy token landed in the YAML.

    Real Groq keys start with the literal ``gsk_`` prefix (per
    https://console.groq.com). A regex match on the raw file body catches
    accidental commits even if the YAML structure was reshuffled.
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    # The substring ``gsk_`` followed by alphanumerics indicates a real
    # Groq API key. The file is allowed to mention ``GROQ_API_KEY`` (the
    # env-var name) anywhere — that's a different string.
    import re

    leak_pattern = re.compile(r"\bgsk_[A-Za-z0-9]{16,}\b")
    matches = leak_pattern.findall(text)
    assert not matches, (
        f"docker-compose.demo.yml appears to contain a real Groq API key: {matches!r}. "
        "Never commit secrets — use an `.env` file (gitignored) instead."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Docker-backed assertion — skips cleanly when daemon is down
# ──────────────────────────────────────────────────────────────────────────────


def test_demo_compose_config_validates() -> None:
    """``docker-compose -f docker-compose.demo.yml config -q`` exits zero.

    Skips cleanly when the Docker daemon isn't reachable — running this
    assertion in CI environments that don't have Docker is acceptable
    because the YAML parse tests above already gate the structural
    invariants.
    """
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(f"docker not available: {reason}")
    # Prefer the dash-separated `docker-compose` (v1 / standalone v2.40.x)
    # because that's what services.yaml and the rest of the project uses.
    # Fall back to `docker compose` (subcommand v2) when only the latter
    # is present.
    bin_candidates = [["docker-compose"], ["docker", "compose"]]
    last_err: subprocess.CalledProcessError | None = None
    for prefix in bin_candidates:
        if prefix[0] != "docker" and shutil.which(prefix[0]) is None:
            continue
        cmd = [*prefix, "-f", str(COMPOSE_FILE), "config", "-q"]
        try:
            proc = subprocess.run(  # noqa: S603 — fully literal argv
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            pytest.skip(f"compose binary {prefix} unusable: {type(exc).__name__}: {exc}")
        if proc.returncode == 0:
            return  # success — compose validated the file
        last_err = subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
    if last_err is not None:
        pytest.fail(f"`docker-compose config -q` exited non-zero (cmd={last_err.cmd}):\nstderr:\n{last_err.stderr}")
    pytest.skip("no compose binary on PATH")
