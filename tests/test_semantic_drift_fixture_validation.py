"""Known-drift fixture validation for the semantic drift-detection engine.

Two layers validate VALID-01 (the guard ships demonstrably trustworthy):

1. DETERMINISTIC offline plumbing tests feed the self-contained fixtures under
   ``tests/fixtures/semantic_drift/{drifted,clean}/`` through the real
   ``review_spec`` pipeline with a SCRIPTED reviewer (a HIGH finding for the
   drifted fixture, ``[]`` for the control) and assert the harness classifies
   detected-HIGH vs clean. These always run offline (no network, no CLI).

2. A LIVE acceptance canary (``test_live_*``) runs the REAL ``claude_reviewer``
   against both fixtures, guarded by ``shutil.which("claude")`` so it skips
   locally without claude and runs as a scheduled-CI canary. It asserts ONLY
   the severity-level outcome (>=1 HIGH drifted, 0 HIGH control) — never the
   model's exact wording, which is non-deterministic.

This phase changes zero ``whilly/`` behavior; the fixtures are standalone
illustrative sources, not real package code.
"""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "scripts" / "semantic_drift_check.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "semantic_drift"

# scripts/ is not an importable package, so load the module by file path.
_spec = importlib.util.spec_from_file_location("semantic_drift_check", _MODULE_PATH)
sdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdc)


def count_high(findings: list[dict]) -> int:
    """Count findings at the highest severity (``sdc.SEVERITIES[0]`` == "HIGH").

    This is the single shared classifier both the deterministic plumbing tests
    and the live canary assert through — define it once, reuse it everywhere.
    """
    high = sdc.SEVERITIES[0]
    return sum(1 for f in findings if f.get("severity") == high)


def _scripted_high_reviewer(captured: dict):
    """Return a reviewer that emits one valid HIGH finding for the drifted slug.

    The finding is built with the seven ``FINDING_KEYS`` (severity HIGH, slug
    "drifted", a TRIAGE_VALUES triage, and ``module.py:<line>`` evidence with a
    colon) so it round-trips ``parse_findings``/``validate_finding`` through the
    real ``review_spec`` pipeline rather than being asserted blindly. The prompt
    handed to the reviewer is captured so the test can prove the real pipeline
    (spec read + matrix resolve + module read + prompt build) actually ran.
    """

    def reviewer(prompt: str) -> str:
        captured["prompt"] = prompt
        finding = {
            "severity": sdc.SEVERITIES[0],  # "HIGH"
            "slug": "drifted",
            "requirement": "The summarize function SHALL return a JSON object.",
            "drift": "summarize returns a bare string instead of a JSON object.",
            "evidence": "module.py:16",
            "triage": sdc.TRIAGE_VALUES[0],  # "code-bug"
            "rationale": "The return statement yields a formatted string, not a dict.",
        }
        return json.dumps([finding])

    return reviewer


def test_plumbing_detects_high_on_drifted_fixture():
    """Drifted fixture + scripted HIGH reviewer -> harness classifies detected-HIGH."""
    captured: dict = {}
    findings = sdc.review_spec(
        "drifted",
        reviewer=_scripted_high_reviewer(captured),
        specs_root=str(_FIXTURES),
        repo_root=str(_FIXTURES / "drifted"),
        matrix_path=str(_FIXTURES / "drifted" / "matrix.md"),
    )

    # Exactly one finding, at the highest severity, classified detected-HIGH.
    assert len(findings) == 1
    assert findings[0]["severity"] == sdc.SEVERITIES[0]
    assert count_high(findings) == 1

    # Prove the real pipeline ran: the prompt embedded the spec SHALL text and
    # the mapped module path (spec read + matrix resolve + module read happened).
    assert "SHALL return a JSON object" in captured["prompt"]
    assert "module.py" in captured["prompt"]


def test_plumbing_reports_clean_on_control_fixture():
    """Clean fixture + scripted '[]' reviewer -> harness classifies clean (zero HIGH)."""
    findings = sdc.review_spec(
        "clean",
        reviewer=lambda _prompt: "[]",
        specs_root=str(_FIXTURES),
        repo_root=str(_FIXTURES / "clean"),
        matrix_path=str(_FIXTURES / "clean" / "matrix.md"),
    )
    assert findings == []
    assert count_high(findings) == 0


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_live_real_claude_flags_drift_and_clears_control():
    """Live canary: the REAL claude_reviewer flags the planted drift, clears the control.

    Asserts ONLY severity-level outcomes (>=1 HIGH on the drifted fixture, 0 HIGH
    on the control). Does NOT assert on the model's wording, drift/rationale text,
    requirement strings, or exact finding counts — those are non-deterministic.
    Skips when claude is not on PATH; runs in scheduled CI as a trustworthiness
    canary.
    """
    drifted = sdc.review_spec(
        "drifted",
        reviewer=sdc.claude_reviewer,
        specs_root=str(_FIXTURES),
        repo_root=str(_FIXTURES / "drifted"),
        matrix_path=str(_FIXTURES / "drifted" / "matrix.md"),
    )
    clean = sdc.review_spec(
        "clean",
        reviewer=sdc.claude_reviewer,
        specs_root=str(_FIXTURES),
        repo_root=str(_FIXTURES / "clean"),
        matrix_path=str(_FIXTURES / "clean" / "matrix.md"),
    )

    assert count_high(drifted) >= 1, "expected the planted drift to flag >=1 HIGH"
    assert count_high(clean) == 0, "control fixture must report zero HIGH (no false positive)"
