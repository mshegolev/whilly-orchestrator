"""Regression test: ``docker-compose.demo.yml`` worker service MUST set
``WHILLY_INSECURE=1`` (truthy) in its ``environment:`` block.

Background — M1 user-testing round-2 finding (fix-m1-event-payload-completeness
worker discovery): with the entrypoint switched over to ``whilly worker
connect`` (the v4-distributed flow), the worker container resolves
``WHILLY_CONTROL_URL=http://control-plane:8000`` — i.e. plain HTTP to a
non-loopback host (``control-plane``). The connect-flow's URL-scheme
guard (:func:`whilly.cli.worker.enforce_scheme_guard`) refuses such a
URL unless the caller explicitly opts in via ``--insecure`` (see
``VAL-M1-INSECURE-*`` assertions).

Without that opt-in the worker container enters a restart loop before
any task is claimed, every demo task stays PENDING, and the new
terminal-state guard at the tail of ``workshop-demo.sh`` correctly
exits 4. The minimal additive fix is to add ``WHILLY_INSECURE: "1"``
to the worker service's ``environment:`` block — the entrypoint's
``is_truthy`` mapping already forwards that as ``--insecure`` to the
inner ``whilly worker connect`` invocation
(:file:`docker/entrypoint.sh` lines 166-170).

This test pins the env var presence + truthy value so a future YAML
edit can't silently regress the demo back into the restart-loop state.

Scope is intentionally narrow: we only assert
1. ``WHILLY_INSECURE`` exists under the ``worker`` service's
   ``environment:`` block, and
2. its value parses as a truthy string per the same ``is_truthy`` rules
   the entrypoint uses (``1 / true / yes / on``, case-insensitive).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
COMPOSE_FILE: Path = REPO_ROOT / "docker-compose.demo.yml"

# Mirrors docker/entrypoint.sh ``is_truthy`` (case-insensitive). The
# entrypoint itself is the source of truth — we keep the matrix here
# small but consistent with that helper.
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def _load_worker_environment() -> dict[str, Any]:
    """Parse compose YAML and return the worker service's environment dict.

    Compose ``environment:`` may legally be either a list of ``KEY=VAL``
    strings or a mapping. We normalize to a ``dict[str, str]`` regardless
    so the assertion below works either way.
    """
    assert COMPOSE_FILE.is_file(), f"missing {COMPOSE_FILE}"
    raw = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "compose file must parse as a mapping"
    services = raw.get("services") or {}
    assert isinstance(services, dict), "compose 'services' must be a mapping"
    worker = services.get("worker")
    assert worker is not None, "compose file is missing the 'worker' service"
    env = worker.get("environment")
    assert env is not None, "worker service is missing 'environment:' block"

    if isinstance(env, dict):
        # YAML mapping → values may be int / bool / None; coerce to str
        # for downstream comparison (compose itself stringifies them).
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    if isinstance(env, list):
        out: dict[str, str] = {}
        for item in env:
            assert isinstance(item, str), f"unexpected env entry: {item!r}"
            if "=" in item:
                k, v = item.split("=", 1)
                out[k] = v
            else:
                # Bare ``KEY`` (compose semantics: pass through from host)
                out[item] = ""
        return out
    raise AssertionError(f"worker.environment has unsupported type: {type(env).__name__}")


def test_worker_environment_has_whilly_insecure() -> None:
    """The worker service's ``environment:`` block MUST contain ``WHILLY_INSECURE``."""
    env = _load_worker_environment()
    assert "WHILLY_INSECURE" in env, (
        "docker-compose.demo.yml worker.environment must declare "
        "WHILLY_INSECURE=1 — without it `whilly worker connect "
        "http://control-plane:8000` is rejected by the scheme guard "
        "and the worker enters a restart loop before claiming any "
        "demo task. See fix-m1-demo-compose-insecure-flag."
    )


def test_worker_whilly_insecure_value_is_truthy() -> None:
    """The declared ``WHILLY_INSECURE`` value MUST be truthy per
    entrypoint's ``is_truthy`` rules.

    Falsy values (``0`` / ``false`` / ``""`` / ``no`` / ``off``) would
    leave the connect-flow in its strict mode and re-introduce the
    restart-loop bug.
    """
    env = _load_worker_environment()
    raw_value = env.get("WHILLY_INSECURE", "")
    # Accept any truthy literal the entrypoint understands. Strip just in
    # case someone writes ``" 1 "`` — bash's is_truthy already trims via
    # the shell-quoted comparison, mirror that conservatively here.
    normalized = str(raw_value).strip().lower()
    assert normalized in _TRUTHY_VALUES, (
        f"docker-compose.demo.yml worker.environment WHILLY_INSECURE="
        f"{raw_value!r} is not truthy. Expected one of "
        f"{sorted(_TRUTHY_VALUES)} so the entrypoint forwards "
        f"--insecure to `whilly worker connect`."
    )


def test_worker_whilly_insecure_grep_assertion() -> None:
    """Grep-style sanity check on the raw YAML text (per feature spec).

    The feature description explicitly asks for a regex grep check —
    keep this in addition to the parsed-YAML assertions so the test
    doubles as a literal raw-text regression: if the line gets
    accidentally commented out, the parsed-dict tests would still
    catch it, but a literal grep gives clearer failure context for
    future readers.
    """
    import re

    text = COMPOSE_FILE.read_text(encoding="utf-8")
    # Match WHILLY_INSECURE: "1" / WHILLY_INSECURE=1 / WHILLY_INSECURE: 1
    # but only when not commented out (line does NOT start with #).
    # Trailing inline ``# ...`` comment is allowed.
    pattern = re.compile(
        r'^(?!\s*#)\s*WHILLY_INSECURE\s*[:=]\s*["\']?1["\']?(?:\s|$|\s*#)',
        re.MULTILINE,
    )
    assert pattern.search(text), (
        'Could not find a non-commented `WHILLY_INSECURE: "1"` (or '
        "equivalent) line in docker-compose.demo.yml. The worker "
        "service's environment block must declare WHILLY_INSECURE=1 "
        "for the demo to function — see fix-m1-demo-compose-insecure-flag."
    )
