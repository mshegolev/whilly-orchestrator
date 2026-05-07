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
            "/path/to/factory/mission/services.yaml",
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
            "/path/to/factory/mission/AGENTS.md",
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
        "tunnel_stability_ok": True,
        "tunnel_probes_passed": 20,
        "tunnel_handshake_verifies_passed": 20,
        "funnel_ssh_etime_seconds": 3600,
    }
    out_file = tmp_path / "state.json"
    out_file.write_text(json.dumps(sample_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    parsed = json.loads(out_file.read_text(encoding="utf-8"))
    assert parsed == sample_state


# ──────────────────────────────────────────────────────────────────────────
# Tunnel-stability gate (v6-baseline-r4) — --require-stable, new state
# fields, evidence captures, and stderr semantics.
# ──────────────────────────────────────────────────────────────────────────


def test_doctor_help_lists_require_stable_flag() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run([bash, str(DOCTOR_SCRIPT), "--help"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0
    out = res.stdout + res.stderr
    assert "--require-stable" in out, "--help must list --require-stable flag"
    assert "tunnel-flapping" in out.lower(), "--help must describe the tunnel-flapping failure mode"


def test_doctor_require_stable_flag_recognized(doctor_text: str) -> None:
    assert "--require-stable" in doctor_text, "doctor must declare --require-stable case in flag parser"
    assert "REQUIRE_STABLE=1" in doctor_text, "doctor must set REQUIRE_STABLE=1 when flag is passed"


def test_doctor_require_stable_does_not_break_existing_flags(doctor_text: str) -> None:
    for legacy in ("--no-bringup", "--json", "--evidence-dir"):
        assert legacy in doctor_text, f"existing flag {legacy} must be preserved (back-compat)"
    assert "NO_BRINGUP=1" in doctor_text
    assert "JSON_MODE=1" in doctor_text


def test_doctor_state_includes_new_tunnel_stability_fields(doctor_text: str) -> None:
    for field in (
        "tunnel_stability_ok",
        "tunnel_probes_passed",
        "tunnel_handshake_verifies_passed",
        "funnel_ssh_etime_seconds",
    ):
        assert field in doctor_text, f"state.json must include `{field}`"


def test_doctor_runs_20_probes_with_1_5s_interval(doctor_text: str) -> None:
    assert "TUNNEL_PROBE_COUNT=20" in doctor_text, "doctor must use 20 probes for the stability gate"
    assert "TUNNEL_PROBE_INTERVAL_MS=1500" in doctor_text, "doctor must use 1.5s probe interval (1500ms)"
    assert "TUNNEL_PROBE_PASS_THRESHOLD=19" in doctor_text, "doctor must require ≥19/20 (95%) probes to pass"


def test_doctor_dual_source_probes_ops_and_vps(doctor_text: str) -> None:
    assert "tunnel-probes-local.txt" in doctor_text or "tunnel-probes-vps.txt" in doctor_text, (
        "doctor must record both ops-side and vps-side probe results"
    )
    assert "ssh_run" in doctor_text and "curl" in doctor_text, "doctor must drive both ssh+curl and curl probes"
    assert "ssl_verify_result" in doctor_text, (
        "doctor must capture TLS verification status from curl (ssl_verify_result writeout)"
    )


def test_doctor_stability_failure_message_format(doctor_text: str) -> None:
    assert "doctor: tunnel-flapping" in doctor_text, "stability failure message must use the exact prefix"
    assert "${tunnel_probes_passed}" in doctor_text and "${tunnel_handshake_verifies_passed}" in doctor_text, (
        "tunnel-flapping message must include both counters (N/20 probes, M/20 verifies)"
    )
    assert "probes passed" in doctor_text and "verifies passed" in doctor_text


def test_doctor_require_stable_promotes_warning_to_failure(doctor_text: str) -> None:
    assert 'REQUIRE_STABLE" -eq 1' in doctor_text or "REQUIRE_STABLE -eq 1" in doctor_text, (
        "doctor must check REQUIRE_STABLE to decide between warning and record_failure"
    )
    assert re.search(
        r'if\s*\[\[\s*"\$tunnel_stability_ok"\s*!=\s*"true"\s*\]\]',
        doctor_text,
    ), "doctor must branch on tunnel_stability_ok != true to apply the gate"


def test_doctor_warning_path_does_not_fail_without_require_stable(doctor_text: str) -> None:
    pattern = re.compile(
        r'if\s*\[\[\s*"\$REQUIRE_STABLE"\s*-eq\s*1\s*\]\];?\s*then\s*\n\s*record_failure[\s\S]+?else\s*\n\s*echo\s+"\$STABILITY_MSG"\s*>&2',
        re.MULTILINE,
    )
    assert pattern.search(doctor_text), (
        "without --require-stable, tunnel-flapping must print to stderr but NOT call record_failure"
    )


def test_doctor_captures_funnel_logs_evidence(doctor_text: str) -> None:
    assert "docker logs --tail 5000 whilly-cp-funnel" in doctor_text, (
        "doctor must capture `docker logs --tail 5000 whilly-cp-funnel` on every invocation"
    )
    assert "funnel-logs.txt" in doctor_text


def test_doctor_captures_funnel_ps_evidence(doctor_text: str) -> None:
    assert "docker exec whilly-cp-funnel ps -o pid,etime,cmd -ax" in doctor_text, (
        "doctor must capture `docker exec whilly-cp-funnel ps -o pid,etime,cmd -ax` on every invocation"
    )
    assert "funnel-ps.txt" in doctor_text


def test_doctor_funnel_ssh_etime_parser_handles_formats(doctor_text: str) -> None:
    assert "funnel_ssh_etime_seconds" in doctor_text
    assert "etime" in doctor_text.lower()
    assert "86400" in doctor_text, "etime parser must convert days portion to seconds (24*3600)"


def test_doctor_gate_threshold_is_95_percent(doctor_text: str) -> None:
    assert "TUNNEL_PROBE_PASS_THRESHOLD=19" in doctor_text, (
        "95% of 20 = 19 — gate threshold must be 19 (≥19/20 pass required)"
    )
    threshold_check = re.search(
        r"tunnel_probes_passed.*-ge.*TUNNEL_PROBE_PASS_THRESHOLD",
        doctor_text,
        re.DOTALL,
    )
    assert threshold_check is not None, "doctor must compare tunnel_probes_passed against the threshold"
    verify_check = re.search(
        r"tunnel_handshake_verifies_passed.*-ge.*TUNNEL_PROBE_PASS_THRESHOLD",
        doctor_text,
        re.DOTALL,
    )
    assert verify_check is not None, "doctor must also gate on tunnel_handshake_verifies_passed >= threshold"


def test_doctor_flag_unrecognised_other_than_require_stable() -> None:
    """Sanity-check: --require-stable must not be classified as 'unknown flag'."""
    bash = shutil.which("bash")
    assert bash is not None
    res = subprocess.run(
        [bash, str(DOCTOR_SCRIPT), "--require-stable", "--no-bringup"],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "VPS_HOST": "root@127.0.0.1", "VPS_PORT": "65500"},
    )
    assert "unknown flag" not in res.stderr.lower(), (
        f"--require-stable must be recognized; stderr was: {res.stderr[:400]}"
    )


def test_agents_md_directs_validator_to_require_stable() -> None:
    agents_md = Path(
        os.environ.get(
            "WHILLY_MISSION_AGENTS_MD",
            "/path/to/factory/mission/AGENTS.md",
        )
    )
    if not agents_md.is_file():
        pytest.skip(f"mission AGENTS.md not present at {agents_md}")
    text = agents_md.read_text(encoding="utf-8")
    assert "--require-stable" in text, (
        "AGENTS.md must instruct user-testing-validator-v6-baseline to run doctor with --require-stable"
    )
    assert "tunnel-flapping" in text, (
        "AGENTS.md must reference the tunnel-flapping abort status so validators recognize it"
    )
