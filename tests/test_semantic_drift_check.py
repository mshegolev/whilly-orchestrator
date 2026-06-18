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


# ---------------------------------------------------------------------------
# Phase 31 Task 1: CLUSTERS partition constant + exhaustive/disjoint live test
# ---------------------------------------------------------------------------

_CLUSTER_NAMES = {
    "orchestration",
    "prd-decision",
    "integrations",
    "operator-surface",
    "platform",
    "safety-quality",
}


def _live_specs_root() -> Path:
    return _REPO_ROOT / "openspec" / "specs"


def _live_slug_set() -> set[str]:
    root = _live_specs_root()
    return {d.name for d in root.iterdir() if (d / "spec.md").is_file()}


def test_clusters_has_exactly_six_named_keys():
    """CLUSTERS has exactly the six canonical cluster names."""
    assert set(sdc.CLUSTERS.keys()) == _CLUSTER_NAMES


def test_clusters_partition_is_exhaustive_vs_live_slugs():
    """The union of all cluster slug lists equals the live openspec/specs slug set."""
    flat = [slug for slugs in sdc.CLUSTERS.values() for slug in slugs]
    assert set(flat) == _live_slug_set()


def test_clusters_partition_is_disjoint_and_thirty_two():
    """No slug appears in two clusters; the partition has exactly 32 unique members."""
    flat = [slug for slugs in sdc.CLUSTERS.values() for slug in slugs]
    assert len(flat) == len(set(flat))
    assert len(set(flat)) == 32


def test_clusters_no_unknown_slugs_on_disk():
    """Every slug listed in CLUSTERS exists as a real openspec/specs/<slug>/spec.md."""
    root = _live_specs_root()
    for slugs in sdc.CLUSTERS.values():
        for slug in slugs:
            assert (root / slug / "spec.md").is_file(), slug


def test_cluster_for_slug_returns_owning_cluster():
    """cluster_for_slug returns the owning cluster name for a known slug."""
    assert sdc.cluster_for_slug("orchestration-loop") == "orchestration"
    assert sdc.cluster_for_slug("prd-wizard") == "prd-decision"
    assert sdc.cluster_for_slug("auth-security") == "platform"
    assert sdc.cluster_for_slug("notifications") == "safety-quality"


def test_cluster_for_slug_unknown_returns_none():
    """An unknown slug returns None (pinned behavior, never raises)."""
    assert sdc.cluster_for_slug("no-such-capability") is None


def test_live_slugs_helper_matches_filesystem():
    """live_slugs() reads the real spec dirs and matches the partition union."""
    assert sdc.live_slugs(specs_root=str(_live_specs_root())) == _live_slug_set()


def test_live_slugs_helper_is_injectable(tmp_path):
    """live_slugs honors an injected specs_root pointing at a fixture tree."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "spec.md").write_text("x", encoding="utf-8")
    (tmp_path / "beta").mkdir()  # no spec.md -> excluded
    assert sdc.live_slugs(specs_root=str(tmp_path)) == {"alpha"}


# ---------------------------------------------------------------------------
# Phase 31 Task 2: run_fleet fan-out + per-unit resilience + run metadata
# ---------------------------------------------------------------------------


def _scaffold_multi_repo(tmp_path: Path, slugs: list[str]) -> tuple[Path, str]:
    """Lay out a fake repo with multiple specs, each mapping one real module."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rows = []
    for slug in slugs:
        _write_spec(repo_root, slug, f"The {slug} system SHALL behave.\n")
        mod = f"whilly/mod_{slug.replace('-', '_')}.py"
        rows.append((mod, slug))
        mod_path = repo_root / mod
        mod_path.parent.mkdir(parents=True, exist_ok=True)
        mod_path.write_text("def f():\n    pass\n", encoding="utf-8")
    _write_matrix(repo_root, rows)
    specs_root = str(repo_root / "openspec" / "specs")
    return repo_root, specs_root


def _patch_clusters(monkeypatch, slugs):
    """Point cluster_for_slug at fixture slugs so run_fleet can tag them."""
    mapping = {slug: f"cluster-{i % 2}" for i, slug in enumerate(slugs)}
    monkeypatch.setattr(sdc, "_SLUG_TO_CLUSTER", mapping)


def test_run_fleet_reviews_every_spec_once(tmp_path, monkeypatch):
    """run_fleet invokes the reviewer for every spec exactly once via the pool."""
    slugs = ["cap-a", "cap-b", "cap-c"]
    repo_root, specs_root = _scaffold_multi_repo(tmp_path, slugs)
    _patch_clusters(monkeypatch, slugs)
    seen = []

    def fake_reviewer(prompt: str) -> str:
        # Identify which slug from the prompt (slug is embedded).
        for s in slugs:
            if f"capability under review: {s}" in prompt.lower():
                seen.append(s)
        return "[]"

    results = sdc.run_fleet(
        slugs,
        reviewer=fake_reviewer,
        max_workers=2,
        specs_root=specs_root,
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert sorted(seen) == sorted(slugs)
    assert set(results["reviewed"]) == set(slugs)
    assert results["errors"] == []
    assert results["findings"] == []


def test_run_fleet_findings_sorted_by_slug_then_severity(tmp_path, monkeypatch):
    """Flattened findings are deterministically sorted by (slug, severity)."""
    slugs = ["cap-b", "cap-a"]
    repo_root, specs_root = _scaffold_multi_repo(tmp_path, slugs)
    _patch_clusters(monkeypatch, slugs)

    def fake_reviewer(prompt: str) -> str:
        # cap-a gets a LOW then HIGH; cap-b gets a MEDIUM.
        if "capability under review: cap-a" in prompt.lower():
            return json.dumps(
                [
                    _valid_finding(slug="cap-a", severity="LOW"),
                    _valid_finding(slug="cap-a", severity="HIGH"),
                ]
            )
        return json.dumps([_valid_finding(slug="cap-b", severity="MEDIUM")])

    def run():
        return sdc.run_fleet(
            slugs,
            reviewer=fake_reviewer,
            max_workers=2,
            specs_root=specs_root,
            repo_root=str(repo_root),
            matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
        )

    r1 = run()
    r2 = run()
    order = [(f["slug"], f["severity"]) for f in r1["findings"]]
    # cap-a before cap-b; within cap-a, HIGH before LOW (SEVERITIES order).
    assert order == [("cap-a", "HIGH"), ("cap-a", "LOW"), ("cap-b", "MEDIUM")]
    assert [(f["slug"], f["severity"]) for f in r2["findings"]] == order


def test_run_fleet_resilient_to_reviewer_exception(tmp_path, monkeypatch):
    """A reviewer that raises for one slug records an error and continues."""
    slugs = ["cap-a", "cap-boom", "cap-c"]
    repo_root, specs_root = _scaffold_multi_repo(tmp_path, slugs)
    _patch_clusters(monkeypatch, slugs)

    def fake_reviewer(prompt: str) -> str:
        if "capability under review: cap-boom" in prompt.lower():
            raise RuntimeError("reviewer blew up")
        return json.dumps([_valid_finding(slug="cap-a")]) if "cap-a" in prompt.lower() else "[]"

    results = sdc.run_fleet(
        slugs,
        reviewer=fake_reviewer,
        max_workers=3,
        specs_root=specs_root,
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert len(results["errors"]) == 1
    err = results["errors"][0]
    assert err["slug"] == "cap-boom"
    assert "cluster" in err
    assert "blew up" in err["error"]
    # The other two specs were still reviewed; findings collected.
    assert set(results["reviewed"]) == {"cap-a", "cap-c"}
    assert any(f["slug"] == "cap-a" for f in results["findings"])


def test_run_fleet_honors_max_workers_bound(tmp_path, monkeypatch):
    """A small max_workers still reviews every spec."""
    slugs = ["cap-a", "cap-b", "cap-c", "cap-d"]
    repo_root, specs_root = _scaffold_multi_repo(tmp_path, slugs)
    _patch_clusters(monkeypatch, slugs)
    results = sdc.run_fleet(
        slugs,
        reviewer=lambda prompt: "[]",
        max_workers=1,
        specs_root=specs_root,
        repo_root=str(repo_root),
        matrix_path=str(repo_root / "openspec" / "COVERAGE-MATRIX.md"),
    )
    assert set(results["reviewed"]) == set(slugs)


def test_collect_run_metadata_uses_injected_git_and_time_seams():
    """Metadata block uses injected git_info + now seams, no real-git dependency."""
    md = sdc.collect_run_metadata(
        model="my-model",
        git_info=lambda: {"commit": "abc123", "dirty": True},
        now=lambda: "2026-06-19T00:00:00Z",
    )
    assert md["model"] == "my-model"
    assert md["commit"] == "abc123"
    assert md["dirty"] is True
    assert md["timestamp"] == "2026-06-19T00:00:00Z"
    assert md["tool_version"] == sdc.TOOL_VERSION


def test_collect_run_metadata_model_resolution_precedence(monkeypatch):
    """model resolves arg > WHILLY_MODEL env > DEFAULT_MODEL."""
    seams = dict(git_info=lambda: {"commit": None, "dirty": None}, now=lambda: "T")
    # Explicit arg wins over env.
    monkeypatch.setenv("WHILLY_MODEL", "env-model")
    assert sdc.collect_run_metadata(model="arg-model", **seams)["model"] == "arg-model"
    # Env wins when no arg.
    assert sdc.collect_run_metadata(model=None, **seams)["model"] == "env-model"
    # Default when neither.
    monkeypatch.delenv("WHILLY_MODEL", raising=False)
    assert sdc.collect_run_metadata(model=None, **seams)["model"] == sdc.DEFAULT_MODEL


def test_collect_run_metadata_default_git_seam_degrades_gracefully(monkeypatch):
    """The default git seam degrades to commit=None/dirty=None on subprocess failure."""

    def boom(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(sdc.subprocess, "run", boom)
    md = sdc.collect_run_metadata(model="m", now=lambda: "T")
    assert md["commit"] is None
    assert md["dirty"] is None
