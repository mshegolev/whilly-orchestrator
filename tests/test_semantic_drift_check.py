"""Unit tests for the model-free core of scripts/semantic_drift_check.py.

These tests are fully offline: no network, no Claude CLI, no subprocess. They
exercise the pure functions that Plan 02 will wire into a live reviewer:
matrix-driven module resolution, the deterministic prompt builder, and the
findings parse/validate pair.
"""

import importlib.util
import json
import shutil
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


# ---------------------------------------------------------------------------
# Plan 02 Task 1: review_spec pipeline with injected reviewer (DETECT-01)
# ---------------------------------------------------------------------------


def _write_spec(repo_root: Path, slug: str, text: str) -> None:
    spec_dir = repo_root / "openspec" / "specs" / slug
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(text, encoding="utf-8")


def _write_matrix(repo_root: Path, rows: list[tuple[str, str]]) -> Path:
    body = "\n".join(f"| {mod} | {cap} | note |" for mod, cap in rows)
    matrix = repo_root / "openspec" / "COVERAGE-MATRIX.md"
    matrix.parent.mkdir(parents=True, exist_ok=True)
    matrix.write_text(
        "## Coverage Matrix\n\n| Module | Capability | Notes |\n|--------|------------|-------|\n" + body + "\n\n",
        encoding="utf-8",
    )
    return matrix


def _scaffold_repo(tmp_path: Path, slug: str = "demo-cap") -> tuple[Path, str]:
    """Lay out a fake repo: spec.md + a matrix mapping one real module path."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_spec(repo_root, slug, "The system SHALL drain the queue completely.\n")
    _write_matrix(repo_root, [("whilly/cli/run.py", slug)])
    src = repo_root / "whilly" / "cli"
    src.mkdir(parents=True)
    (src / "run.py").write_text("def run():\n    pass\n", encoding="utf-8")
    return repo_root, slug


def test_review_spec_returns_findings_for_valid_reviewer(tmp_path):
    """A fake reviewer returning a canned valid finding round-trips through review_spec."""
    repo_root, slug = _scaffold_repo(tmp_path)
    captured = {}

    def fake_reviewer(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps([_valid_finding(slug=slug)])

    findings = sdc.review_spec(
        slug,
        reviewer=fake_reviewer,
        specs_root=str(repo_root / "openspec" / "specs"),
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert len(findings) == 1
    assert findings[0]["slug"] == slug
    # The prompt must contain the slug and at least one mapped module path.
    assert slug in captured["prompt"]
    assert "whilly/cli/run.py" in captured["prompt"]


def test_review_spec_clean_spec_returns_empty(tmp_path):
    """A fake reviewer returning '[]' yields [] (clean spec, no network)."""
    repo_root, slug = _scaffold_repo(tmp_path)
    findings = sdc.review_spec(
        slug,
        reviewer=lambda prompt: "[]",
        specs_root=str(repo_root / "openspec" / "specs"),
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert findings == []


def test_review_spec_junk_output_returns_empty(tmp_path):
    """A fake reviewer returning non-JSON junk yields [] (report-only, no raise)."""
    repo_root, slug = _scaffold_repo(tmp_path)
    findings = sdc.review_spec(
        slug,
        reviewer=lambda prompt: "I could not analyze this, sorry <<<",
        specs_root=str(repo_root / "openspec" / "specs"),
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert findings == []


def test_review_spec_missing_slug_returns_empty(tmp_path):
    """A slug whose spec.md is missing returns [] and never calls the reviewer."""
    repo_root, _ = _scaffold_repo(tmp_path)
    called = {"n": 0}

    def fake_reviewer(prompt: str) -> str:
        called["n"] += 1
        return "[]"

    findings = sdc.review_spec(
        "no-such-capability",
        reviewer=fake_reviewer,
        specs_root=str(repo_root / "openspec" / "specs"),
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert findings == []
    assert called["n"] == 0


def test_review_spec_skips_missing_module_sources(tmp_path):
    """A mapped module that does not exist on disk is skipped, not fatal."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    slug = "demo-cap"
    _write_spec(repo_root, slug, "The system SHALL do X.\n")
    _write_matrix(repo_root, [("whilly/ghost.py", slug)])  # never created on disk
    captured = {}

    def fake_reviewer(prompt: str) -> str:
        captured["prompt"] = prompt
        return "[]"

    findings = sdc.review_spec(
        slug,
        reviewer=fake_reviewer,
        specs_root=str(repo_root / "openspec" / "specs"),
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert findings == []
    # Prompt still built; the missing module is referenced (recorded as unreadable).
    assert "whilly/ghost.py" in captured["prompt"]


# ---------------------------------------------------------------------------
# Plan 02 Task 2: default Claude-CLI reviewer + --slug CLI main (DETECT-01)
# ---------------------------------------------------------------------------


def test_main_prints_findings_json_and_exits_zero(tmp_path, capsys):
    """main(--slug ...) with an injected fake reviewer prints findings JSON, returns 0."""
    repo_root, slug = _scaffold_repo(tmp_path)

    def fake_reviewer(prompt: str) -> str:
        return json.dumps([_valid_finding(slug=slug)])

    rc = sdc.main(
        [
            "--slug",
            slug,
            "--specs-root",
            str(repo_root / "openspec" / "specs"),
            "--repo-root",
            str(repo_root),
            "--matrix-path",
            str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
        ],
        reviewer=fake_reviewer,
    )
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["slug"] == slug


def test_main_clean_spec_exits_zero(tmp_path, capsys):
    """The CLI returns 0 and prints [] when the reviewer reports no drift."""
    repo_root, slug = _scaffold_repo(tmp_path)
    rc = sdc.main(
        [
            "--slug",
            slug,
            "--specs-root",
            str(repo_root / "openspec" / "specs"),
            "--repo-root",
            str(repo_root),
            "--matrix-path",
            str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
        ],
        reviewer=lambda prompt: "[]",
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_bad_slug_exits_zero_with_empty_array(tmp_path, capsys):
    """A missing/invalid --slug logs to stderr but exits 0 with [] on stdout."""
    repo_root, _ = _scaffold_repo(tmp_path)
    rc = sdc.main(
        [
            "--slug",
            "does-not-exist",
            "--specs-root",
            str(repo_root / "openspec" / "specs"),
            "--repo-root",
            str(repo_root),
            "--matrix-path",
            str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
        ],
        reviewer=lambda prompt: "[]",
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []


def test_claude_reviewer_builds_expected_argv(monkeypatch):
    """claude_reviewer builds argv with --output-format json, -p, and honors CLAUDE_BIN."""
    monkeypatch.setenv("CLAUDE_BIN", "my-fake-claude")
    captured = {}

    class _Result:
        stdout = json.dumps({"result": "[]"})
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(sdc.subprocess, "run", fake_run)
    out = sdc.claude_reviewer("PROMPT-TEXT")
    cmd = captured["cmd"]
    assert cmd[0] == "my-fake-claude"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "PROMPT-TEXT"
    # Envelope unwrap: the inner "result" string is returned, not the raw JSON.
    assert out == "[]"


def test_claude_reviewer_falls_back_to_raw_stdout_on_unexpected_envelope(monkeypatch):
    """If stdout is not the expected {result: ...} envelope, return raw stdout."""

    class _Result:
        stdout = "[]"  # plain array, not an envelope
        returncode = 0

    monkeypatch.setattr(sdc.subprocess, "run", lambda cmd, **kwargs: _Result())
    assert sdc.claude_reviewer("PROMPT") == "[]"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_live_cli_reviewer_runs_against_real_claude(tmp_path):
    """Live path: only runs when claude is installed; asserts review_spec returns a list."""
    repo_root, slug = _scaffold_repo(tmp_path)
    findings = sdc.review_spec(
        slug,
        reviewer=sdc.claude_reviewer,
        specs_root=str(repo_root / "openspec" / "specs"),
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert isinstance(findings, list)
