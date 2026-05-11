"""M3 — default image tag in distributed compose files must match current release.

Background
----------
The user-testing round 1 root cause for the M3 HTMX/DEMO-BLOCKED swarm
was a stale default image tag in the two distributed compose files:

* ``docker-compose.control-plane.yml`` pinned
  ``image: ${WHILLY_IMAGE:-mshegolev/whilly:4.3.1}``
* ``docker-compose.worker.yml`` pinned the same 4.3.1 default
* ``.env.worker.example`` carried the same 4.3.1 commented hint

When validators / operators ran ``docker-compose -f docker-compose.control-plane.yml
up -d`` WITHOUT explicitly exporting ``WHILLY_IMAGE``, compose pulled
the 4.3.1 image — which doesn't know alembic migrations
``010_funnel_url`` (M2) and ``011_events_notify_trigger`` (M3). The
control-plane crashed on boot with::

    alembic.util.exc.CommandError: Can't locate revision identified by
    '010_funnel_url'

…and entered a restart loop, blocking 47 downstream HTMX/DEMO assertions.

Fix-m3-compose-default-image-tag-bump bumped the default to
``mshegolev/whilly:4.6.0`` (the LIVE release at fix time, equal to
``whilly.__version__`` then). The v4.6.1 patch release lifted that
default again to ``mshegolev/whilly:4.6.1`` so operators pulling the
unpinned default get the M3 user-facing fix bundle (UPPERCASE SSE
event names, tasks-API 400 validation, metrics stale plan-label
cleanup, Last-Event-ID overflow guard, listener_connected health
flag). This module pins both halves of that contract:

1. Both compose files declare an image string of the literal form
   ``${WHILLY_IMAGE:-mshegolev/whilly:<X.Y.Z>}`` — so an unset
   ``WHILLY_IMAGE`` falls back to a published Docker Hub tag.
2. The fallback tag's version >= ``whilly.__version__``. This is the
   forward-compat guard: when v4.7.0 lands, whoever bumps
   ``whilly/__init__.py`` MUST also bump these compose defaults
   together, or this test fails. (The reverse — defaults *ahead* of
   ``__version__`` — is allowed because the published image always
   exists at any tag <= the latest release.)
3. ``.env.worker.example`` keeps a commented ``WHILLY_IMAGE=...``
   hint that is also >= ``whilly.__version__`` — so an operator who
   uncomments the hint gets a working pull.
4. None of the three files still reference the broken 4.3.1 default.

The tests are pure-text / pure-YAML — no docker daemon needed, no
network access — so they always run, even on developer machines
without compose installed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from whilly import __version__ as WHILLY_VERSION

yaml = pytest.importorskip("yaml")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
CONTROL_PLANE_FILE: Path = REPO_ROOT / "docker-compose.control-plane.yml"
WORKER_FILE: Path = REPO_ROOT / "docker-compose.worker.yml"
ENV_EXAMPLE_FILE: Path = REPO_ROOT / ".env.worker.example"

STALE_TAG = "mshegolev/whilly:4.3.1"

_IMAGE_LITERAL_RE = re.compile(r"^\$\{WHILLY_IMAGE:-mshegolev/whilly:(?P<tag>\d+\.\d+\.\d+)\}$")
_ENV_HINT_RE = re.compile(
    r"^#\s*WHILLY_IMAGE=mshegolev/whilly:(?P<tag>\d+\.\d+\.\d+)\s*$",
    re.MULTILINE,
)


def _parse_semver(s: str) -> tuple[int, int, int]:
    parts = s.split(".")
    assert len(parts) == 3, f"expected MAJOR.MINOR.PATCH, got {s!r}"
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _service_image(compose_path: Path, service_name: str) -> str:
    raw = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{compose_path.name} must parse as a mapping"
    services = raw.get("services") or {}
    svc = services.get(service_name)
    assert svc is not None, f"{compose_path.name}: missing service '{service_name}'"
    image = svc.get("image")
    assert isinstance(image, str), f"{compose_path.name}: services.{service_name}.image must be a string; got {image!r}"
    return image


# ──────────────────────────────────────────────────────────────────────────────
# Shape: image string is the parametrised ${WHILLY_IMAGE:-mshegolev/whilly:X.Y.Z}
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "compose_path,service_name",
    [
        (CONTROL_PLANE_FILE, "control-plane"),
        (WORKER_FILE, "worker"),
    ],
    ids=["control-plane", "worker"],
)
def test_image_uses_whilly_image_var_with_published_default(compose_path: Path, service_name: str) -> None:
    """The image: line must be ``${WHILLY_IMAGE:-mshegolev/whilly:X.Y.Z}``.

    Operators that ``export WHILLY_IMAGE=whilly:dev`` get their override.
    Operators that don't export anything fall back to a published tag.
    Anything else (bare ``mshegolev/whilly:4.6.1``, or a non-mshegolev
    fallback) silently breaks one of those two paths.
    """
    image = _service_image(compose_path, service_name)
    match = _IMAGE_LITERAL_RE.match(image)
    assert match is not None, (
        f"{compose_path.name}: services.{service_name}.image must be of the form "
        f"'${{WHILLY_IMAGE:-mshegolev/whilly:<X.Y.Z>}}' so an unset env var "
        f"falls back to a published Docker Hub tag; got {image!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tag freshness: default >= current whilly.__version__
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "compose_path,service_name",
    [
        (CONTROL_PLANE_FILE, "control-plane"),
        (WORKER_FILE, "worker"),
    ],
    ids=["control-plane", "worker"],
)
def test_image_default_tag_at_or_above_current_version(compose_path: Path, service_name: str) -> None:
    """The default image tag must be >= whilly.__version__.

    Forward-compat guard. When the next release (v4.7.0+) bumps
    ``whilly/__init__.py``, this test fails until the same release
    bumps the default tag in both compose files. The original M3
    user-testing failure was exactly this kind of drift — the package
    advanced through M2/M3 migrations while the compose default stayed
    pinned to the 4.3.1 image, and operators running ``up -d`` without
    setting ``WHILLY_IMAGE`` got a stale image that crashed on
    migrations 010_funnel_url / 011_events_notify_trigger.
    """
    image = _service_image(compose_path, service_name)
    match = _IMAGE_LITERAL_RE.match(image)
    assert match is not None, f"{compose_path.name}: image must match the parametrised literal first; got {image!r}"
    default_tag = match.group("tag")
    default_semver = _parse_semver(default_tag)
    current_semver = _parse_semver(WHILLY_VERSION)
    assert default_semver >= current_semver, (
        f"{compose_path.name}: services.{service_name}.image default tag "
        f"{default_tag!r} is OLDER than whilly.__version__ ({WHILLY_VERSION!r}). "
        f"This is exactly the M3 user-testing root cause: a compose default "
        f"frozen behind the package version pulls a published image that "
        f"doesn't know newer alembic migrations and crashes the "
        f"control-plane on `up -d`. Bump the default to "
        f"`mshegolev/whilly:{WHILLY_VERSION}` (or higher) when releasing."
    )


# ──────────────────────────────────────────────────────────────────────────────
# .env.worker.example: commented hint must be in lock-step with the compose default
# ──────────────────────────────────────────────────────────────────────────────


def test_env_worker_example_image_hint_at_or_above_current_version() -> None:
    """``.env.worker.example`` must keep its ``# WHILLY_IMAGE=...`` hint
    at a tag >= whilly.__version__.

    Operators copy ``.env.worker.example`` to ``.env.worker`` and
    uncomment whichever lines they want to override. If the commented
    hint points at a stale tag, an operator who uncomments it without
    thinking gets the same broken-image experience this fix prevents.
    """
    text = ENV_EXAMPLE_FILE.read_text(encoding="utf-8")
    matches = _ENV_HINT_RE.findall(text)
    assert matches, (
        f"{ENV_EXAMPLE_FILE.name}: expected a commented '# WHILLY_IMAGE=mshegolev/whilly:<X.Y.Z>' hint; none found."
    )
    for tag in matches:
        hint_semver = _parse_semver(tag)
        current_semver = _parse_semver(WHILLY_VERSION)
        assert hint_semver >= current_semver, (
            f"{ENV_EXAMPLE_FILE.name}: commented WHILLY_IMAGE hint pins "
            f"{tag!r}, which is OLDER than whilly.__version__ "
            f"({WHILLY_VERSION!r}). Bump it in lock-step with the compose "
            f"defaults so operators who uncomment the hint don't pull a "
            f"pre-migration image."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Regression: the broken 4.3.1 default must not reappear anywhere
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [CONTROL_PLANE_FILE, WORKER_FILE, ENV_EXAMPLE_FILE],
    ids=["control-plane.yml", "worker.yml", ".env.worker.example"],
)
def test_no_stale_4_3_1_default(path: Path) -> None:
    """The pre-fix 4.3.1 tag must not reappear in any of the three files.

    Defence-in-depth: the version-floor check above already catches
    drift, but a hand-pin to ``mshegolev/whilly:4.3.1`` is the exact
    pattern the M3 user-testing round 1 found, and re-introducing it
    re-introduces the migration-revision-missing crash.
    """
    text = path.read_text(encoding="utf-8")
    assert STALE_TAG not in text, (
        f"{path.name} still references {STALE_TAG!r}, the pre-fix default "
        f"that crashes the control-plane on alembic migration "
        f"010_funnel_url / 011_events_notify_trigger. Bump to the current "
        f"published tag (>= {WHILLY_VERSION!r})."
    )
