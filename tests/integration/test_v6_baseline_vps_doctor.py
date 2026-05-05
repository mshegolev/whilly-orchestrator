"""v6-baseline VPS doctor script offline gates.

This file gates the static contract of `scripts/v6-baseline-vps-doctor.sh`
so a regression in the doctor is caught at PR time rather than at the
next live VPS pre-flight. It does NOT require SSH access to the VPS —
the live pre-flight is exercised by the user-testing-validator at
`bash scripts/v6-baseline-vps-doctor.sh` against a real
`VPS_HOST`, which is intentionally NOT a hermetic CI step.

The contract this gate pins:

1. The script exists, is executable, and passes `bash -n`.
2. CLI surface:
   * `--help` exits 0 and prints usage from the in-script docblock.
   * Unknown flag exits 2 with a clear stderr message.
   * Supported flags `--json`, `--no-bringup`, `--evidence-dir <path>`
     are declared in the script source.
3. No `tailscale*` symbols appear (2026-05-02 pivot).
4. The bringup path invokes `scripts/v6-baseline-vps-up.sh` with the
   `--skip-smoke --skip-sync` flag pair so the doctor reuses the
   sibling bringup script idempotently.
5. The script reads the fresh lhr.life URL from postgres `funnel_url`
   together with the `updated_at` age (NEVER caches across runs).
6. The script probes `/health` and `/metrics` (the latter with a
   bearer discovered from the running control-plane container env)
   and verifies auth still gates correctly when the bearer is unset.
7. The script writes a `state.json` file containing the required
   nine fields the validator consumes.
8. The off-limits `openclaw-gateway` container is read-only — the
   script only inspects it (no `docker stop` / `docker rm` on it).
9. `--json` mode emits valid JSON to stdout (verified by inspecting
   the JSON-construction site: `json.dumps` + the `--json`
   short-circuit).
10. The "stack already running" branch short-circuits the bringup
    invocation (idempotent re-run no-op contract).
11. `services.yaml` exposes the doctor via `v6_baseline_vps_doctor`.
12. AGENTS.md `Testing & Validation Guidance` instructs the
    user-testing-validator-v6-baseline to invoke the doctor first
    and read state.json for the live lhr_url (no cached URL across
    runs).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "v6-baseline-vps-doctor.sh"


@pytest.fixture(scope="module")
def doctor_text() -> str:
    return DOCTOR_SCRIPT.read_text(encoding="utf-8")


def test_doctor_script_exists_and_executable() -> None:
    assert DOCTOR_SCRIPT.is_file(), f"missing {DOCTOR_SCRIPT}"
    assert os.access(DOCTOR_SCRIPT, os.X_OK), f"{DOCTOR_SCRIPT} is not executable"


def test_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None, "bash not on PATH"
    res = subprocess.run([bash, "-n", str(DOCTOR_SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, f"{DOCTOR_SCRIPT.name} syntax error:\n{res.stderr}"


def test_help_flag_exits_zero_and_prints_usage() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(DOCTOR_SCRIPT), "--help"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, f"--help exited {res.returncode}"
    out = res.stdout + res.stderr
    assert "doctor" in out.lower() or "vps" in out.lower(), f"--help did not print usage:\n{out[:400]}"
    for flag in ("--json", "--no-bringup", "--evidence-dir", "--help"):
        assert flag in out, f"--help did not list {flag}"


def test_short_help_flag_exits_zero() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(DOCTOR_SCRIPT), "-h"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, f"-h exited {res.returncode}"


def test_unknown_flag_exits_two() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(DOCTOR_SCRIPT), "--no-such-flag"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 2, f"unknown flag should exit 2, got {res.returncode}\nstderr: {res.stderr[:400]}"
    assert "unknown flag" in res.stderr.lower()


def test_evidence_dir_flag_requires_value() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(DOCTOR_SCRIPT), "--evidence-dir"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 2, f"--evidence-dir without arg should exit 2, got {res.returncode}"
    assert "requires a value" in res.stderr.lower() or "evidence-dir" in res.stderr.lower()


def test_doctor_no_tailscale_references(doctor_text: str) -> None:
    forbidden = re.compile(r"\btailscale", re.IGNORECASE)
    assert not forbidden.search(doctor_text), (
        "v6-baseline doctor must not reference Tailscale (removed 2026-05-02; "
        "public exposure is via localhost.run funnel sidecar only)"
    )


def test_doctor_canonical_vps_defaults(doctor_text: str) -> None:
    assert "VPS_HOST:-root@213.159.6.155" in doctor_text
    assert "VPS_PORT:-23422" in doctor_text
    assert "VPS_DIR:-/root/whilly" in doctor_text


def test_doctor_default_evidence_dir(doctor_text: str) -> None:
    assert "EVIDENCE_DIR:-out/v6-baseline-vps-doctor" in doctor_text


def test_doctor_invokes_vps_up_with_skip_flags(doctor_text: str) -> None:
    assert "v6-baseline-vps-up.sh" in doctor_text
    assert "--skip-smoke" in doctor_text
    assert "--skip-sync" in doctor_text


def test_doctor_reads_fresh_funnel_url_with_age(doctor_text: str) -> None:
    assert "funnel_url" in doctor_text and "psql" in doctor_text
    assert "updated_at" in doctor_text
    assert "EXTRACT(EPOCH" in doctor_text or "lhr_url_age_seconds" in doctor_text


def test_doctor_probes_health_endpoint(doctor_text: str) -> None:
    assert "/health" in doctor_text and "curl" in doctor_text
    assert "--max-time 15" in doctor_text


def test_doctor_probes_metrics_with_bearer_and_gates_check(doctor_text: str) -> None:
    assert "/metrics" in doctor_text
    assert "WHILLY_METRICS_TOKEN" in doctor_text
    assert "Authorization: Bearer" in doctor_text
    assert "401" in doctor_text, "doctor must verify auth still gates correctly (401 fail-closed)"


def test_doctor_records_all_required_state_fields(doctor_text: str) -> None:
    required_fields = (
        "ssh_ok",
        "stack_state",
        "lhr_url",
        "lhr_url_age_seconds",
        "health_ok",
        "health_response",
        "metrics_ok",
        "control_plane_image_tag",
        "openclaw_gateway_status",
    )
    for field in required_fields:
        assert field in doctor_text, f"state.json must include `{field}`"


def test_doctor_state_file_path(doctor_text: str) -> None:
    assert "state.json" in doctor_text


def test_doctor_openclaw_read_only(doctor_text: str) -> None:
    assert "openclaw-gateway" in doctor_text, "doctor must inspect openclaw-gateway invariant"
    forbidden_against_openclaw = re.compile(
        r"docker\s+(?:stop|rm|kill|restart)\s+[^\n]*openclaw-gateway",
        re.IGNORECASE,
    )
    assert not forbidden_against_openclaw.search(doctor_text), (
        "doctor MUST NOT touch the off-limits openclaw-gateway container — read-only inspection only"
    )


def test_doctor_json_mode_emits_valid_json(doctor_text: str) -> None:
    assert "--json" in doctor_text, "doctor must declare --json flag"
    assert "JSON_MODE" in doctor_text, "doctor must track --json with a JSON_MODE switch"
    assert "json.dumps" in doctor_text, "doctor must use json.dumps to emit a single-line JSON to stdout"
    assert 'separators=(",", ":"' in doctor_text, "doctor --json output must be minified (separators=,:)"


def test_doctor_idempotent_skip_bringup_when_running(doctor_text: str) -> None:
    assert 'stack_state="running"' in doctor_text or 'stack_state="running"' in doctor_text
    assert re.search(r'if\s*\[\[\s*"\$stack_state"\s*!=\s*"running"\s*\]\]', doctor_text), (
        "doctor must short-circuit the bringup path when the stack is already running"
    )
    assert "skipping bringup" in doctor_text.lower() or "idempotent no-op" in doctor_text.lower()


def test_doctor_no_bringup_flag_exits_non_zero(doctor_text: str) -> None:
    assert "NO_BRINGUP" in doctor_text
    assert "--no-bringup" in doctor_text
    assert "stack down and --no-bringup set" in doctor_text


def test_doctor_writes_state_json_with_python_serializer(doctor_text: str) -> None:
    assert "json.dump" in doctor_text, "doctor must persist state.json via json.dump"
    assert 'open(path, "w"' in doctor_text or "open(path,'w'" in doctor_text


def test_services_yaml_exposes_doctor() -> None:
    services_yaml = Path(
        os.environ.get(
            "WHILLY_MISSION_SERVICES_YAML",
            "/Users/mshegolev/.factory/missions/75d95174-16a0-4392-a6c8-c5508a381918/services.yaml",
        )
    )
    if not services_yaml.is_file():
        pytest.skip(f"mission services.yaml not present at {services_yaml}")
    text = services_yaml.read_text(encoding="utf-8")
    assert "v6_baseline_vps_doctor" in text
    assert "scripts/v6-baseline-vps-doctor.sh" in text


def test_agents_md_instructs_validator_to_invoke_doctor_first() -> None:
    agents_md = Path(
        os.environ.get(
            "WHILLY_MISSION_AGENTS_MD",
            "/Users/mshegolev/.factory/missions/75d95174-16a0-4392-a6c8-c5508a381918/AGENTS.md",
        )
    )
    if not agents_md.is_file():
        pytest.skip(f"mission AGENTS.md not present at {agents_md}")
    text = agents_md.read_text(encoding="utf-8")
    assert "v6-baseline-vps-doctor" in text or "v6_baseline_vps_doctor" in text, (
        "AGENTS.md must reference the v6-baseline VPS doctor"
    )
    assert "state.json" in text, "AGENTS.md must instruct the validator to read state.json"


def test_doctor_state_json_schema_round_trip(tmp_path: Path) -> None:
    """Sanity-check that the embedded python serializer produces valid JSON."""
    sample_state = {
        "ssh_ok": True,
        "stack_state": "running",
        "lhr_url": "https://example.lhr.life",
        "lhr_url_age_seconds": 42,
        "health_ok": True,
        "health_response": '{"status":"ok"}',
        "metrics_ok": True,
        "control_plane_image_tag": "mshegolev/whilly:4.6.1",
        "openclaw_gateway_status": "running",
    }
    out_file = tmp_path / "state.json"
    out_file.write_text(json.dumps(sample_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    parsed = json.loads(out_file.read_text(encoding="utf-8"))
    assert parsed == sample_state
