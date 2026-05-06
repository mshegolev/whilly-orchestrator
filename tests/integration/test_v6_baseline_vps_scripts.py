"""v6-baseline VPS bringup/teardown script offline gates.

This file gates the static contract of `scripts/v6-baseline-vps-up.sh`
and `scripts/v6-baseline-vps-down.sh` so a regression in those scripts
is caught at PR time rather than at the next live VPS bringup. It does
NOT require SSH access to the VPS — the live multi-host smoke is
covered by the operator-facing `bash scripts/v6-baseline-vps-up.sh`
invocation with a real `VPS_HOST` set, which is intentionally NOT a
hermetic CI step (it needs the operator's SSH key + a reachable VPS).

The contract this gate pins:

1. Both scripts exist, are executable, and pass `bash -n` syntax check.
2. The bringup script:
   * declares a `--help` flag that exits 0 and prints usage from the
     in-script docblock;
   * fails with a clear error on unknown flags (exit code 2);
   * uses the canonical defaults `VPS_HOST=root@213.159.6.155`,
     `VPS_PORT=23422`, `WHILLY_IMAGE_TAG=4.6.1`;
   * preserves the `--profile funnel` invocation so the localhost.run
     sidecar joins the compose run;
   * never references any `tailscale*` symbol (per 2026-05-02 pivot —
     Tailscale is REMOVED).
3. The teardown script:
   * declares a `--help` flag and rejects unknown flags;
   * runs `docker compose ... down` with optional `-v` (volumes);
   * preserves the off-limits openclaw-gateway invariant (a check
     that the container is still running at the end).
4. `services.yaml` exposes the two scripts via the
   `v6_baseline_vps_up` / `v6_baseline_vps_down` command keys so
   future workers discover them through the manifest.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_COMPOSE: Path = REPO_ROOT / "docker-compose.control-plane.yml"
UP_SCRIPT = REPO_ROOT / "scripts" / "v6-baseline-vps-up.sh"
DOWN_SCRIPT = REPO_ROOT / "scripts" / "v6-baseline-vps-down.sh"


@pytest.fixture(scope="module")
def up_text() -> str:
    return UP_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def down_text() -> str:
    return DOWN_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def control_plane_compose_text() -> str:
    return CONTROL_PLANE_COMPOSE.read_text(encoding="utf-8")


def test_up_script_exists_and_executable() -> None:
    assert UP_SCRIPT.is_file(), f"missing {UP_SCRIPT}"
    assert os.access(UP_SCRIPT, os.X_OK), f"{UP_SCRIPT} is not executable"


def test_down_script_exists_and_executable() -> None:
    assert DOWN_SCRIPT.is_file(), f"missing {DOWN_SCRIPT}"
    assert os.access(DOWN_SCRIPT, os.X_OK), f"{DOWN_SCRIPT} is not executable"


@pytest.mark.parametrize("script", [UP_SCRIPT, DOWN_SCRIPT])
def test_bash_syntax(script: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None, "bash not on PATH"
    res = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, f"{script.name} syntax error:\n{res.stderr}"


@pytest.mark.parametrize("script", [UP_SCRIPT, DOWN_SCRIPT])
def test_help_flag_exits_zero_and_prints_usage(script: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(script), "--help"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, f"{script.name} --help exited {res.returncode}"
    out = res.stdout + res.stderr
    assert "v6" in out.lower() or "vps" in out.lower(), f"{script.name} --help did not print usage:\n{out[:400]}"


@pytest.mark.parametrize("script", [UP_SCRIPT, DOWN_SCRIPT])
def test_unknown_flag_exits_two(script: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(script), "--no-such-flag"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 2, (
        f"{script.name} --no-such-flag should exit 2 (env misuse), got {res.returncode}\nstderr: {res.stderr[:400]}"
    )
    assert "unknown flag" in res.stderr.lower()


def test_up_default_image_floor_is_v6_baseline(up_text: str) -> None:
    assert "WHILLY_IMAGE_TAG:-4.6.1" in up_text, (
        "v6-baseline floor must default to mshegolev/whilly:4.6.1 "
        "(LIVE on Docker Hub, multi-arch, per mission preconditions)"
    )


def test_up_canonical_vps_defaults(up_text: str) -> None:
    assert "VPS_HOST:-root@213.159.6.155" in up_text
    assert "VPS_PORT:-23422" in up_text
    assert "VPS_DIR:-/root/whilly" in up_text


def test_up_uses_funnel_profile(up_text: str) -> None:
    assert "--profile funnel" in up_text, (
        "the v6-baseline topology requires the localhost.run funnel sidecar — "
        "compose must be invoked with --profile funnel so the sidecar joins the run"
    )


def test_up_invokes_compose_control_plane_yml(up_text: str) -> None:
    assert "docker-compose.control-plane.yml" in up_text


def test_up_installs_private_metrics_bearer_env_file(up_text: str) -> None:
    assert "WHILLY_METRICS_ENV_FILE" in up_text
    assert "WHILLY_METRICS_TOKEN" in up_text
    assert "V6_BASELINE_METRICS_TOKEN" in up_text
    assert "secrets.token_urlsafe" in up_text
    assert "--env-file '$WHILLY_METRICS_ENV_FILE'" in up_text
    assert "WHILLY_METRICS_TOKEN masked" in up_text


def test_control_plane_compose_wires_metrics_token_env(control_plane_compose_text: str) -> None:
    assert "WHILLY_METRICS_TOKEN: ${WHILLY_METRICS_TOKEN:-}" in control_plane_compose_text


def test_up_resolves_stable_url_from_lhr_hostname(up_text: str) -> None:
    """v6.0 paid plan pins the URL — env-driven LHR_HOSTNAME, no postgres lookup."""
    assert "LHR_HOSTNAME" in up_text
    assert "https://${LHR_HOSTNAME}" in up_text


def test_up_health_probe_through_public_url(up_text: str) -> None:
    assert "/health" in up_text and "curl" in up_text


def test_up_no_tailscale_references(up_text: str) -> None:
    # 2026-05-02 pivot: Tailscale is REMOVED. Any reference is a regression.
    forbidden = re.compile(r"\btailscale", re.IGNORECASE)
    assert not forbidden.search(up_text), (
        "v6-baseline UP script must not reference Tailscale (removed 2026-05-02; "
        "public exposure is via localhost.run funnel sidecar only)"
    )


def test_up_preserves_openclaw_gateway_status_check(up_text: str) -> None:
    assert "openclaw-gateway" in up_text, (
        "the bringup script should record openclaw-gateway status to confirm the off-limits :18789 service is preserved"
    )


def test_up_evidence_dir_default(up_text: str) -> None:
    assert "EVIDENCE_DIR:-out/v6-baseline-vps-up" in up_text


def test_down_runs_compose_down(down_text: str) -> None:
    assert "docker compose -f docker-compose.control-plane.yml --profile funnel down" in down_text


def test_down_volumes_flag(down_text: str) -> None:
    assert "--volumes" in down_text and "-v" in down_text


def test_down_preserves_openclaw_invariant(down_text: str) -> None:
    assert "openclaw-gateway" in down_text, (
        "teardown script must verify openclaw-gateway is still running "
        "(off-limits invariant per AGENTS.md mission boundaries)"
    )


def test_down_no_tailscale_references(down_text: str) -> None:
    forbidden = re.compile(r"\btailscale", re.IGNORECASE)
    assert not forbidden.search(down_text)


def test_down_evidence_dir_default(down_text: str) -> None:
    assert "EVIDENCE_DIR:-out/v6-baseline-vps-down" in down_text


def test_services_yaml_exposes_both_scripts() -> None:
    services_yaml = Path(
        os.environ.get(
            "WHILLY_MISSION_SERVICES_YAML",
            "/Users/m.v.shchegolev/.factory/missions/75d95174-16a0-4392-a6c8-c5508a381918/services.yaml",
        )
    )
    if not services_yaml.is_file():
        pytest.skip(f"mission services.yaml not present at {services_yaml}")
    text = services_yaml.read_text(encoding="utf-8")
    assert "v6_baseline_vps_up" in text
    assert "v6_baseline_vps_down" in text
    assert "scripts/v6-baseline-vps-up.sh" in text
    assert "scripts/v6-baseline-vps-down.sh" in text
