"""Unit tests for the model-free core of scripts/semantic_drift_check.py.

These tests are fully offline: no network, no Claude CLI, no subprocess. They
exercise the pure functions that Plan 02 will wire into a live reviewer:
matrix-driven module resolution, the deterministic prompt builder, and the
findings parse/validate pair.
"""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "scripts" / "semantic_drift_check.py"

# scripts/ is not an importable package, so load the module by file path.
_spec = importlib.util.spec_from_file_location("semantic_drift_check", _MODULE_PATH)
sdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdc)


# ---------------------------------------------------------------------------
# Task 1: matrix-driven module resolution (DETECT-04)
# ---------------------------------------------------------------------------


def test_resolve_modules_for_slug_orchestration_loop_live_matrix():
    """orchestration-loop resolves to its 9 mapped module paths from the live matrix."""
    matrix_path = _REPO_ROOT / "openspec" / "COVERAGE-MATRIX.md"
    modules = sdc.resolve_modules_for_slug("orchestration-loop", matrix_path=str(matrix_path))

    # Count of "| orchestration-loop |" body rows in the live matrix.
    body_rows = [
        line for line in matrix_path.read_text(encoding="utf-8").splitlines() if "| orchestration-loop |" in line
    ]
    assert len(modules) == len(body_rows)
    assert "whilly/cli/run.py" in modules
    assert "whilly/core/models.py" in modules


def test_resolve_modules_for_slug_is_live(tmp_path):
    """Resolution reads the matrix at call time: a temp matrix changes the result."""
    matrix = tmp_path / "matrix.md"
    matrix.write_text(
        "## Coverage Matrix\n\n"
        "| Module | Capability | Notes |\n"
        "|--------|------------|-------|\n"
        "| whilly/a.py | x | note a |\n"
        "| whilly/b.py | x | note b |\n"
        "| whilly/c.py | y | note c |\n\n",
        encoding="utf-8",
    )
    modules = sdc.resolve_modules_for_slug("x", matrix_path=str(matrix))
    assert modules == ["whilly/a.py", "whilly/b.py"]


def test_resolve_modules_for_slug_unknown_returns_empty(tmp_path):
    """An unknown slug returns an empty list without raising."""
    matrix = tmp_path / "matrix.md"
    matrix.write_text(
        "## Coverage Matrix\n\n"
        "| Module | Capability | Notes |\n"
        "|--------|------------|-------|\n"
        "| whilly/a.py | x | note a |\n\n",
        encoding="utf-8",
    )
    assert sdc.resolve_modules_for_slug("does-not-exist", matrix_path=str(matrix)) == []


def test_resolve_modules_for_slug_exact_match_not_substring(tmp_path):
    """Slug matching is exact: 'orchestration' must not match 'orchestration-loop'."""
    matrix = tmp_path / "matrix.md"
    matrix.write_text(
        "## Coverage Matrix\n\n"
        "| Module | Capability | Notes |\n"
        "|--------|------------|-------|\n"
        "| whilly/a.py | orchestration-loop | note a |\n"
        "| whilly/b.py | UNMAPPED | note b |\n\n",
        encoding="utf-8",
    )
    # Substring of a real capability must not resolve anything.
    assert sdc.resolve_modules_for_slug("orchestration", matrix_path=str(matrix)) == []
    # UNMAPPED is matched exactly like any other slug, never auto-returned for others.
    assert sdc.resolve_modules_for_slug("orchestration-loop", matrix_path=str(matrix)) == ["whilly/a.py"]


# ---------------------------------------------------------------------------
# Task 2: pure review-prompt builder (DETECT-02 / DETECT-03 prompt contract)
# ---------------------------------------------------------------------------


def _sample_sources():
    return [
        ("whilly/cli/run.py", "def run():\n    pass\n"),
        ("whilly/core/models.py", "class Task:\n    pass\n"),
    ]


def test_build_review_prompt_is_pure_and_deterministic():
    """Identical inputs yield byte-identical output (no I/O, no nondeterminism)."""
    spec_text = "The system SHALL drain the queue.\n"
    a = sdc.build_review_prompt("orchestration-loop", spec_text, _sample_sources())
    b = sdc.build_review_prompt("orchestration-loop", spec_text, _sample_sources())
    assert a == b
    assert isinstance(a, str)


def test_build_review_prompt_embeds_slug_spec_and_sources():
    """The prompt embeds the slug, the full spec text, and each module path + source."""
    spec_text = "The system SHALL drain the queue completely.\n"
    prompt = sdc.build_review_prompt("orchestration-loop", spec_text, _sample_sources())
    assert "orchestration-loop" in prompt
    assert "The system SHALL drain the queue completely." in prompt
    for path, source in _sample_sources():
        assert path in prompt
        assert source.strip() in prompt


def test_build_review_prompt_names_schema_keys_and_strict_json():
    """The prompt demands a strict JSON array and names every required finding key."""
    prompt = sdc.build_review_prompt("s", "spec", _sample_sources())
    for key in ("severity", "slug", "requirement", "drift", "evidence", "triage", "rationale"):
        assert key in prompt
    assert "JSON" in prompt
    assert "array" in prompt.lower()


def test_build_review_prompt_names_enums_and_file_line():
    """The prompt names severities, triage values, and demands file:line evidence."""
    prompt = sdc.build_review_prompt("s", "spec", _sample_sources())
    for sev in ("HIGH", "MEDIUM", "LOW"):
        assert sev in prompt
    assert "code-bug" in prompt
    assert "spec-overstatement" in prompt
    assert "file:line" in prompt


def test_build_review_prompt_states_clean_spec_empty_array():
    """The prompt states that a spec with no drift returns an empty array []."""
    prompt = sdc.build_review_prompt("s", "spec", _sample_sources())
    assert "[]" in prompt


# ---------------------------------------------------------------------------
# Task 3: findings parse + per-finding validate (DETECT-02, DETECT-03)
# ---------------------------------------------------------------------------


def _valid_finding(**overrides):
    finding = {
        "severity": "HIGH",
        "slug": "orchestration-loop",
        "requirement": "The system SHALL drain the queue.",
        "drift": "Loop exits before queue is empty.",
        "evidence": "whilly/cli/run.py:42",
        "triage": "code-bug",
        "rationale": "The break condition fires on first None claim.",
    }
    finding.update(overrides)
    return finding


def test_parse_findings_single_valid_array():
    import json

    text = json.dumps([_valid_finding()])
    findings = sdc.parse_findings(text)
    assert len(findings) == 1
    assert findings[0]["evidence"] == "whilly/cli/run.py:42"


def test_parse_findings_empty_array_clean_spec():
    assert sdc.parse_findings("[]") == []


def test_parse_findings_tolerates_fenced_block_and_prose():
    import json

    payload = json.dumps([_valid_finding()])
    text = f"Here is my review:\n```json\n{payload}\n```\nThanks!"
    findings = sdc.parse_findings(text)
    assert len(findings) == 1
    assert findings[0]["triage"] == "code-bug"


def test_parse_findings_json_repair_recovers_trailing_comma():
    import json

    pytest.importorskip("json_repair")
    payload = json.dumps([_valid_finding()])
    # Inject a trailing comma to break strict json.loads but stay repairable.
    broken = payload[:-1] + ",]"
    findings = sdc.parse_findings(broken)
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"


def test_parse_findings_returns_empty_on_unrecoverable():
    assert sdc.parse_findings("not json at all <<<") == []


def test_parse_findings_drops_invalid_entries():
    import json

    valid = _valid_finding()
    invalid = _valid_finding(severity="CRITICAL")
    findings = sdc.parse_findings(json.dumps([valid, invalid]))
    assert findings == [valid]


def test_validate_finding_accepts_complete_valid_finding():
    assert sdc.validate_finding(_valid_finding()) is True


def test_validate_finding_rejects_bad_severity():
    assert sdc.validate_finding(_valid_finding(severity="CRITICAL")) is False


def test_validate_finding_rejects_bad_triage():
    assert sdc.validate_finding(_valid_finding(triage="unknown")) is False


def test_validate_finding_rejects_missing_key():
    finding = _valid_finding()
    del finding["rationale"]
    assert sdc.validate_finding(finding) is False


def test_validate_finding_rejects_evidence_without_colon():
    assert sdc.validate_finding(_valid_finding(evidence="whilly/cli/run.py")) is False
