"""Integration test: ``docker-compose.demo.yml`` honours ``WHILLY_IMAGE_TAG_REF``.

Verifies the contract behind VAL-M1-COMPOSE-011: operators can pin the
demo stack to a published ``mshegolev/whilly:<tag>`` image by setting
``WHILLY_IMAGE_TAG=<tag>`` (which the ``workshop-demo.sh`` wrapper
expands into ``WHILLY_IMAGE_TAG_REF=mshegolev/whilly:<tag>`` and exports
into the compose process).

Three layers of assertions:

1. **YAML-only.** The three demo services (control-plane, worker, seed)
   must each declare ``image: ${WHILLY_IMAGE_TAG_REF:-whilly-demo:latest}``
   so unset → byte-equivalent behaviour and set → published image.

2. **Compose-resolved (set).** ``WHILLY_IMAGE_TAG_REF=mshegolev/whilly:4.4.1
   docker-compose -f docker-compose.demo.yml --profile seed config`` resolves
   every demo-service image field to ``mshegolev/whilly:4.4.1``.

3. **Compose-resolved (unset).** With ``WHILLY_IMAGE_TAG_REF`` unset,
   the same command resolves to ``whilly-demo:latest`` — preserving
   v4.4.1 backwards-compat for operators who run ``docker build``.

The Docker-backed checks skip cleanly when the daemon isn't reachable
(matches the existing ``test_demo_compose_default_env.py`` pattern).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
COMPOSE_FILE: Path = REPO_ROOT / "docker-compose.demo.yml"

EXPECTED_IMAGE_LITERAL = "${WHILLY_IMAGE_TAG_REF:-whilly-demo:latest}"
LOCAL_FALLBACK = "whilly-demo:latest"
PUBLISHED_TAG = "mshegolev/whilly:4.4.1"

DEMO_SERVICES_WITH_WHILLY_IMAGE = ("control-plane", "worker", "seed")


def _load_compose() -> dict:
    assert COMPOSE_FILE.is_file(), f"missing {COMPOSE_FILE}"
    raw = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "compose file must parse as a mapping"
    services = raw.get("services") or {}
    assert isinstance(services, dict), "services: block must be a mapping"
    return services


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
        return False, f"`docker info` exited {proc.returncode}"
    return True, "ok"


def _compose_prefix() -> list[str] | None:
    """Pick the first compose binary that is actually on PATH."""
    if shutil.which("docker-compose") is not None:
        return ["docker-compose"]
    if shutil.which("docker") is not None:
        return ["docker", "compose"]
    return None


def _run_compose_config(env: dict[str, str]) -> str:
    """Run ``compose -f docker-compose.demo.yml --profile seed config`` and
    return stdout. Skips cleanly if Docker / compose is unusable."""
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(f"docker not available: {reason}")
    prefix = _compose_prefix()
    if prefix is None:
        pytest.skip("no compose binary on PATH")
    cmd = [*prefix, "-f", str(COMPOSE_FILE), "--profile", "seed", "config"]
    proc = subprocess.run(  # noqa: S603 — fully literal argv
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, **env},
    )
    if proc.returncode != 0:
        pytest.fail(f"`{' '.join(cmd)}` exited {proc.returncode}\nstderr:\n{proc.stderr}")
    return proc.stdout


# ──────────────────────────────────────────────────────────────────────────────
# YAML-only assertions — always run, no Docker needed
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("service_name", DEMO_SERVICES_WITH_WHILLY_IMAGE)
def test_service_image_uses_whilly_image_tag_ref_with_local_fallback(service_name: str) -> None:
    """control-plane / worker / seed must each use the parametrised image string.

    Backs VAL-M1-COMPOSE-011: ``WHILLY_IMAGE_TAG_REF`` selects the image,
    ``whilly-demo:latest`` is the fallback when the var is unset.
    """
    services = _load_compose()
    svc = services.get(service_name)
    assert svc is not None, f"compose file is missing the '{service_name}' service"
    image = svc.get("image")
    assert image == EXPECTED_IMAGE_LITERAL, (
        f"services.{service_name}.image must be {EXPECTED_IMAGE_LITERAL!r} (VAL-M1-COMPOSE-011); got: {image!r}"
    )


def test_no_demo_service_hardcodes_whilly_demo_latest() -> None:
    """Defence-in-depth: no service should still hardcode ``whilly-demo:latest``.

    The fallback lives inside the ``${...:-whilly-demo:latest}`` expansion
    on the parametrised services. Any bare ``image: whilly-demo:latest``
    line would silently bypass the env-var wiring.
    """
    services = _load_compose()
    for name in DEMO_SERVICES_WITH_WHILLY_IMAGE:
        svc = services.get(name) or {}
        image = svc.get("image")
        assert image != LOCAL_FALLBACK, (
            f"services.{name}.image must not bare-reference {LOCAL_FALLBACK!r}; "
            f"use {EXPECTED_IMAGE_LITERAL!r} so WHILLY_IMAGE_TAG_REF can override it."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Compose-resolved assertions — skip when Docker isn't available
# ──────────────────────────────────────────────────────────────────────────────


def test_compose_config_resolves_to_published_tag_when_set() -> None:
    """``WHILLY_IMAGE_TAG_REF=mshegolev/whilly:4.4.1`` → all 3 services use it."""
    out = _run_compose_config({"WHILLY_IMAGE_TAG_REF": PUBLISHED_TAG})
    image_lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("image:")]
    whilly_image_lines = [ln for ln in image_lines if "whilly" in ln]
    assert len(whilly_image_lines) >= len(DEMO_SERVICES_WITH_WHILLY_IMAGE), (
        f"expected at least {len(DEMO_SERVICES_WITH_WHILLY_IMAGE)} whilly-image lines "
        f"in compose config output; got: {whilly_image_lines!r}"
    )
    for ln in whilly_image_lines:
        assert PUBLISHED_TAG in ln, (
            f"compose config did not resolve image to {PUBLISHED_TAG!r}; line: {ln!r}\nfull output:\n{out}"
        )
    for ln in whilly_image_lines:
        assert LOCAL_FALLBACK not in ln, (
            f"compose config still mentions {LOCAL_FALLBACK!r} when WHILLY_IMAGE_TAG_REF is set; line: {ln!r}"
        )


def test_compose_config_falls_back_to_local_tag_when_unset() -> None:
    """Unset ``WHILLY_IMAGE_TAG_REF`` → all 3 services use ``whilly-demo:latest``."""
    env = {k: v for k, v in os.environ.items() if k != "WHILLY_IMAGE_TAG_REF"}
    out = _run_compose_config(env)
    image_lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("image:")]
    whilly_image_lines = [ln for ln in image_lines if "whilly-demo" in ln or "mshegolev" in ln]
    assert len(whilly_image_lines) >= len(DEMO_SERVICES_WITH_WHILLY_IMAGE), (
        f"expected at least {len(DEMO_SERVICES_WITH_WHILLY_IMAGE)} whilly-image lines "
        f"in compose config output; got: {whilly_image_lines!r}\nfull output:\n{out}"
    )
    for ln in whilly_image_lines:
        assert LOCAL_FALLBACK in ln, (
            f"compose config did not fall back to {LOCAL_FALLBACK!r}; line: {ln!r}\nfull output:\n{out}"
        )
        assert "mshegolev/whilly" not in ln, (
            f"compose config resolved to a published tag despite WHILLY_IMAGE_TAG_REF being unset; line: {ln!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# workshop-demo.sh wrapper assertions
# ──────────────────────────────────────────────────────────────────────────────


def test_workshop_demo_sh_exports_whilly_image_tag_ref() -> None:
    """The wrapper must compute and export ``WHILLY_IMAGE_TAG_REF`` from
    ``WHILLY_IMAGE_TAG`` so compose picks it up without a separate
    ``--env-file``."""
    text = (REPO_ROOT / "workshop-demo.sh").read_text(encoding="utf-8")
    assert "WHILLY_IMAGE_TAG" in text, "workshop-demo.sh must reference WHILLY_IMAGE_TAG"
    assert "export WHILLY_IMAGE_TAG_REF" in text, (
        "workshop-demo.sh must `export WHILLY_IMAGE_TAG_REF=...` so docker-compose inherits it (VAL-M1-COMPOSE-011)."
    )
    assert "mshegolev/whilly:" in text, (
        "workshop-demo.sh must build the published image reference as `mshegolev/whilly:${WHILLY_IMAGE_TAG}`."
    )
    assert "docker manifest inspect" in text, (
        "workshop-demo.sh must fail-fast via `docker manifest inspect` "
        "when WHILLY_IMAGE_TAG points at a non-existent tag."
    )
    assert "docker pull" in text, (
        "workshop-demo.sh must `docker pull` the published image instead of "
        "`docker build` when WHILLY_IMAGE_TAG is set."
    )
