"""Unit tests for the M1 readiness-baseline fixtures (mission v5.0).

These tests pin the *shape* of the baseline artifacts written by
``scripts/m1_baseline_fixtures.py``:

* ``tests/fixtures/v3_tasks.json`` — pre-key_files plan.
* ``tests/fixtures/v4_tasks.json`` — v4.0-era plan with key_files +
  dependencies + plan_id.
* ``tests/fixtures/baselines/events_payload_v4.3.1.json`` — JSON-Schema
  baseline for the v4.3.1 ``events.payload`` jsonb column.
* ``tests/fixtures/whilly_state-v4.3.json`` — frozen state-store snapshot
  with the v4.3.x field set.

They also exercise :func:`tests.conftest.load_fixture` so the helper's
contract is locked in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FIXTURES_DIR, load_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_audit_source(repo_root: Path = REPO_ROOT) -> Path | None:
    """Return the canonical audit-report source for the current checkout.

    Mirrors the resolution chain in ``scripts/m1_baseline_fixtures.py``:

    1. ``.planning/distributed-audit/`` — mission-local working copy
       (may be untracked, missing on a clean clone / CI runner).
    2. ``library/distributed-audit/`` — tracked canonical M1 mirror per
       VAL-M1-DOCS-004.

    Returns ``None`` when neither path contains any files, in which case
    callers must ``pytest.skip(...)`` per the AGENTS.md "Test hygiene" rule.
    """
    candidates = (
        repo_root / ".planning" / "distributed-audit",
        repo_root / "library" / "distributed-audit",
    )
    for candidate in candidates:
        if candidate.is_dir() and any(p.is_file() for p in candidate.iterdir()):
            return candidate
    return None


def test_load_fixture_returns_parsed_json_for_json_files() -> None:
    """``.json`` fixtures are returned already parsed (dict / list)."""
    data = load_fixture("v3_tasks.json")
    assert isinstance(data, dict)
    assert "tasks" in data and isinstance(data["tasks"], list)


def test_load_fixture_supports_subdirectory_paths() -> None:
    """Names may include sub-paths under ``tests/fixtures/``."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    assert isinstance(data, dict)
    assert data.get("version") == "4.3.1"


def test_load_fixture_raises_for_missing_file() -> None:
    """Missing fixtures raise :class:`FileNotFoundError` with a clear path."""
    with pytest.raises(FileNotFoundError):
        load_fixture("does-not-exist.json")


def test_load_fixture_returns_text_for_non_json_files(tmp_path) -> None:
    """Non-``.json`` files are returned as a UTF-8 string.

    Uses a temp file copied into FIXTURES_DIR via monkeypatching is
    overkill; instead we pick an existing markdown asset under
    ``docs/distributed-audit/`` via the public copy path, but since we
    don't want to depend on doc names, this test creates a one-off
    fixture under the existing ``tests/fixtures/`` tree and removes it.
    """
    target = FIXTURES_DIR / "_unit_smoke.txt"
    target.write_text("hello\n", encoding="utf-8")
    try:
        text = load_fixture("_unit_smoke.txt")
    finally:
        target.unlink(missing_ok=True)
    assert isinstance(text, str)
    assert text == "hello\n"


# ─── v3 tasks fixture ────────────────────────────────────────────────────


def test_v3_tasks_fixture_has_no_key_files_per_task() -> None:
    """v3-era plans must not carry the ``key_files`` field on tasks."""
    data = load_fixture("v3_tasks.json")
    assert all("key_files" not in t for t in data["tasks"])


def test_v3_tasks_fixture_has_legacy_required_task_fields() -> None:
    """v3 tasks still carry id/status/priority/dependencies."""
    expected = {"id", "phase", "category", "priority", "description", "status", "dependencies"}
    for task in load_fixture("v3_tasks.json")["tasks"]:
        assert expected.issubset(task.keys())


# ─── v4 tasks fixture ────────────────────────────────────────────────────


def test_v4_tasks_fixture_has_plan_id() -> None:
    """v4.0-era plans gained a top-level ``plan_id`` field."""
    data = load_fixture("v4_tasks.json")
    assert isinstance(data.get("plan_id"), str) and data["plan_id"]


def test_v4_tasks_fixture_has_key_files_and_dependencies_per_task() -> None:
    """v4 tasks must carry both key_files and dependencies."""
    for task in load_fixture("v4_tasks.json")["tasks"]:
        assert "key_files" in task and isinstance(task["key_files"], list)
        assert "dependencies" in task and isinstance(task["dependencies"], list)


def test_v4_tasks_fixture_has_at_least_one_dependent_task() -> None:
    """At least one task must depend on another to exercise the planner."""
    deps_present = [t for t in load_fixture("v4_tasks.json")["tasks"] if t["dependencies"]]
    assert deps_present, "v4 fixture must include at least one dependent task"


# ─── events.payload baseline ─────────────────────────────────────────────


def test_events_payload_baseline_pins_v4_3_1_event_types() -> None:
    """Baseline must enumerate all canonical v4.3.1 event_type entries."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    types = set(data["event_types"].keys())
    expected_subset = {
        "CLAIM",
        "COMPLETE",
        "FAIL",
        "RELEASE",
        "RESET",
        "task.created",
        "task.skipped",
        "plan.applied",
        "plan.budget_exceeded",
        "triz.contradiction",
        "triz.error",
    }
    assert expected_subset.issubset(types), f"missing event_types: {expected_subset - types}"


def test_events_payload_baseline_each_entry_has_object_schema() -> None:
    """Every event_type entry must be an object-typed JSON-Schema fragment."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    for name, schema in data["event_types"].items():
        assert schema.get("type") == "object", f"{name}: type != object"


def test_events_payload_baseline_release_reasons_pinned() -> None:
    """RELEASE.reason enum must include the canonical v4.3.1 reasons."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    release = data["event_types"]["RELEASE"]
    enum = release["properties"]["reason"]["enum"]
    assert {"visibility_timeout", "worker_offline"}.issubset(enum)


# ─── events.payload v4.4.0 baseline (enriched shape) ─────────────────────
#
# The v4.4.0 baseline documents the enriched shape introduced by the
# M1 fix for VAL-CROSS-BACKCOMPAT-909..-912: CLAIM / COMPLETE / FAIL /
# RELEASE event payloads now carry ``worker_id`` + ``task_id`` +
# ``plan_id`` at minimum, plus event-specific extras
# (CLAIM: ``claimed_at``; COMPLETE: ``usage``; FAIL: ``error``;
# RELEASE: enum extended with ``admin_revoked``). The v4.3.1 baseline
# above is preserved as a forward-readability regression anchor —
# any v4.3.1 reader that validated ``additionalProperties: true``
# accepts v4.4.0 payloads, so legacy data still parses.


def test_events_payload_v4_4_0_baseline_exists_and_pins_version() -> None:
    """The v4.4.0 baseline must exist alongside v4.3.1 and report the new version."""
    data = load_fixture("baselines/events_payload_v4.4.0.json")
    assert data.get("version") == "4.4.0"
    assert data.get("supersedes") == "events_payload_v4.3.1.json"


def test_events_payload_v4_4_0_baseline_pins_enriched_required_keys() -> None:
    """CLAIM / COMPLETE / FAIL / RELEASE must require the v4.4.0 enriched key set."""
    data = load_fixture("baselines/events_payload_v4.4.0.json")
    expected_required = {
        "CLAIM": {"worker_id", "task_id", "plan_id", "claimed_at", "version"},
        "COMPLETE": {"worker_id", "task_id", "plan_id", "version", "usage"},
        "FAIL": {"worker_id", "task_id", "plan_id", "version", "reason", "error"},
        "RELEASE": {"worker_id", "task_id", "plan_id", "version", "reason"},
    }
    for event_type, required_keys in expected_required.items():
        schema = data["event_types"][event_type]
        actual_required = set(schema["required"])
        assert required_keys.issubset(actual_required), (
            f"{event_type}: required={actual_required}; missing {required_keys - actual_required}"
        )


def test_events_payload_v4_4_0_baseline_is_strict_superset_of_v4_3_1() -> None:
    """Every v4.3.1 required key must remain required in v4.4.0 (forward-readability)."""
    legacy = load_fixture("baselines/events_payload_v4.3.1.json")
    enriched = load_fixture("baselines/events_payload_v4.4.0.json")
    for event_type, legacy_schema in legacy["event_types"].items():
        if event_type not in enriched["event_types"]:
            continue
        legacy_required = set(legacy_schema.get("required", []))
        enriched_required = set(enriched["event_types"][event_type].get("required", []))
        assert legacy_required.issubset(enriched_required), (
            f"{event_type}: v4.4.0 dropped a v4.3.1 required key — missing {legacy_required - enriched_required}"
        )


def test_events_payload_v4_4_0_release_reason_enum_extends_v4_3_1() -> None:
    """v4.4.0 RELEASE.reason enum must extend (not replace) the v4.3.1 enum."""
    legacy = load_fixture("baselines/events_payload_v4.3.1.json")
    enriched = load_fixture("baselines/events_payload_v4.4.0.json")
    legacy_enum = set(legacy["event_types"]["RELEASE"]["properties"]["reason"]["enum"])
    enriched_enum = set(enriched["event_types"]["RELEASE"]["properties"]["reason"]["enum"])
    assert legacy_enum.issubset(enriched_enum), (
        f"v4.4.0 RELEASE.reason enum dropped values: {legacy_enum - enriched_enum}"
    )
    # ``admin_revoked`` is the new additive value pinned by VAL-CROSS-BACKCOMPAT-912.
    assert "admin_revoked" in enriched_enum


def test_events_payload_v4_3_1_baseline_unchanged_as_legacy_anchor() -> None:
    """The v4.3.1 baseline is the legacy regression anchor and must stay frozen.

    The required-key sets pinned here are the *legacy* shape — fresh
    rows emitted by v4.4.0+ code paths use the v4.4.0 baseline above,
    but already-emitted rows in long-running databases must still
    parse against this baseline. Tests that validate freshly-emitted
    rows MUST use the v4.4.0 baseline; tests that validate legacy
    fixture data MUST use this v4.3.1 baseline.
    """
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    legacy_required = {
        "CLAIM": {"worker_id", "version"},
        "COMPLETE": {"version"},
        "FAIL": {"version", "reason"},
        "RELEASE": {"version", "reason"},
    }
    for event_type, required_keys in legacy_required.items():
        schema = data["event_types"][event_type]
        assert set(schema["required"]) == required_keys, (
            f"v4.3.1 {event_type} required-set drift: got {schema['required']}, expected {sorted(required_keys)}"
        )


# ─── whilly_state-v4.3.json snapshot ─────────────────────────────────────


def test_whilly_state_snapshot_has_v4_3_field_set() -> None:
    """Frozen state snapshot must carry every field the StateStore writes."""
    data = load_fixture("whilly_state-v4.3.json")
    expected = {
        "plan_file",
        "iteration",
        "cost_usd",
        "active_agents",
        "task_status",
        "paused",
        "pause_reason",
        "paused_at",
        "saved_at",
    }
    assert expected.issubset(data.keys()), f"missing: {expected - data.keys()}"


def test_whilly_state_snapshot_active_agents_have_session_name() -> None:
    """Active-agents entries need session_name + task_id (v4.3 contract)."""
    data = load_fixture("whilly_state-v4.3.json")
    for agent in data["active_agents"]:
        assert {"task_id", "session_name"}.issubset(agent.keys())


def test_whilly_state_snapshot_round_trips_through_state_store() -> None:
    """The snapshot must load cleanly through :class:`whilly.state_store.StateStore`."""
    import json
    import time

    from whilly.state_store import StateStore

    data = load_fixture("whilly_state-v4.3.json")

    # The on-disk snapshot is stale by design; bump saved_at to "now" so
    # StateStore.load doesn't reject it as >24h old.
    data["saved_at"] = time.time()

    store = StateStore(state_file=str(FIXTURES_DIR / "_unit_smoke_state.json"))
    try:
        store.state_file.write_text(json.dumps(data), encoding="utf-8")
        loaded = store.load()
        assert loaded is not None
        assert loaded["iteration"] == data["iteration"]
        assert loaded["task_status"] == data["task_status"]
        assert loaded["active_agents"] == data["active_agents"]
    finally:
        store.clear()


# ─── docs/distributed-audit mirror ───────────────────────────────────────


def test_distributed_audit_docs_mirror_canonical_source() -> None:
    """``docs/distributed-audit/`` must mirror the canonical audit source.

    Source is resolved via the fallback chain documented on
    :func:`_resolve_audit_source`:

    * Prefer ``.planning/distributed-audit/`` (mission-local working copy).
    * Fall back to ``library/distributed-audit/`` (tracked canonical
      mirror per VAL-M1-DOCS-004) when ``.planning/`` is absent — this is
      the situation on a clean clone or CI runner.

    Skipped cleanly if neither source path is reachable in this
    environment, per the AGENTS.md "Test hygiene" rule.
    """
    src = _resolve_audit_source()
    if src is None:
        pytest.skip(
            "Neither .planning/distributed-audit/ nor library/distributed-audit/ "
            "is populated in this environment — nothing to mirror against."
        )
    dst = REPO_ROOT / "docs" / "distributed-audit"
    assert dst.is_dir(), f"missing docs mirror: {dst}"
    src_names = {p.name for p in src.iterdir() if p.is_file()}
    dst_names = {p.name for p in dst.iterdir() if p.is_file()}
    assert src_names == dst_names, f"only-in-src: {src_names - dst_names}, only-in-dst: {dst_names - src_names}"
    for name in src_names:
        assert (src / name).read_bytes() == (dst / name).read_bytes(), f"drift: {name}"


def test_distributed_audit_library_mirror_canonical_source() -> None:
    """``library/distributed-audit/`` must mirror the canonical audit source.

    This is the canonical M1 location required by VAL-M1-DOCS-004 /
    VAL-M1-COMPOSE-902, populated by the m1-docs feature via the same
    idempotent ``scripts/m1_baseline_fixtures.py`` writer that maintains
    the legacy ``docs/distributed-audit/`` mirror.

    Source resolution mirrors the script: prefer ``.planning/`` then fall
    back to ``library/``. When ``library/`` IS the resolved source (i.e.
    we're on a clean clone with no ``.planning/``), this test still
    validates the canonical mirror is non-empty and self-consistent.
    """
    src = _resolve_audit_source()
    if src is None:
        pytest.skip(
            "Neither .planning/distributed-audit/ nor library/distributed-audit/ "
            "is populated in this environment — nothing to mirror against."
        )
    dst = REPO_ROOT / "library" / "distributed-audit"
    assert dst.is_dir(), f"missing library mirror: {dst}"
    dst_names = {p.name for p in dst.iterdir() if p.is_file()}
    assert dst_names, "library/distributed-audit/ is empty"
    if src.resolve() == dst.resolve():
        # Library IS the source — nothing further to compare.
        return
    src_names = {p.name for p in src.iterdir() if p.is_file()}
    assert src_names == dst_names, f"only-in-src: {src_names - dst_names}, only-in-dst: {dst_names - src_names}"
    for name in src_names:
        assert (src / name).read_bytes() == (dst / name).read_bytes(), f"drift: {name}"


def test_m1_baseline_fixtures_script_is_idempotent_on_rerun(tmp_path) -> None:
    """Re-running ``scripts/m1_baseline_fixtures.py`` on a clean checkout is a no-op.

    Captures the script's stdout summary table over two consecutive
    invocations and asserts the second pass reports nothing as
    ``created`` or ``updated`` — every action line ends with
    ``unchanged`` instead.
    """
    import subprocess

    script = REPO_ROOT / "scripts" / "m1_baseline_fixtures.py"
    # First run primes any drift; the assertion runs on the second pass
    # so this test never trips on a fresh clone where the fixtures
    # legitimately need creating.
    subprocess.run(["python3", str(script)], cwd=REPO_ROOT, check=True, capture_output=True)
    second = subprocess.run(["python3", str(script)], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    for line in second.stdout.splitlines():
        if not line.strip():
            continue
        # Each action line is "  <name>  <status>  <relpath>"; status is
        # the second whitespace-separated field after collapsing.
        assert "created" not in line.split() and "updated" not in line.split(), (
            f"non-idempotent re-run produced action line: {line!r}"
        )


# ─── Regression: clean-clone fallback chain ──────────────────────────────


def _build_synthetic_repo(tmp_root: Path, *, with_planning: bool, with_library: bool) -> Path:
    """Construct a minimal synthetic repo tree under ``tmp_root``.

    Always copies the real script into ``scripts/m1_baseline_fixtures.py``.
    Optionally mirrors the *real* ``library/distributed-audit/`` files into
    the synthetic library/ and/or .planning/ directory so the regression
    tests work against the same canonical content the dev repo uses.
    Returns the synthetic repo root (== ``tmp_root``).
    """
    import shutil

    (tmp_root / "scripts").mkdir(parents=True)
    real_script = REPO_ROOT / "scripts" / "m1_baseline_fixtures.py"
    shutil.copy2(real_script, tmp_root / "scripts" / "m1_baseline_fixtures.py")

    real_library = REPO_ROOT / "library" / "distributed-audit"
    if not real_library.is_dir() or not any(p.is_file() for p in real_library.iterdir()):
        pytest.skip(
            "Real library/distributed-audit/ is empty in this environment; "
            "synthetic-tree regression test cannot run without canonical content."
        )

    if with_library:
        synthetic_library = tmp_root / "library" / "distributed-audit"
        synthetic_library.mkdir(parents=True)
        for f in real_library.iterdir():
            if f.is_file():
                shutil.copy2(f, synthetic_library / f.name)

    if with_planning:
        synthetic_planning = tmp_root / ".planning" / "distributed-audit"
        synthetic_planning.mkdir(parents=True)
        for f in real_library.iterdir():
            if f.is_file():
                shutil.copy2(f, synthetic_planning / f.name)

    return tmp_root


def _run_synthetic_script(synthetic_root: Path) -> tuple[int, str, str]:
    """Run ``scripts/m1_baseline_fixtures.py`` against a synthetic repo root.

    Honours the ``WHILLY_M1_BASELINE_ROOT`` env var the script supports for
    test isolation. Returns ``(exit_code, stdout, stderr)``.
    """
    import os
    import subprocess

    env = os.environ.copy()
    env["WHILLY_M1_BASELINE_ROOT"] = str(synthetic_root)
    result = subprocess.run(
        ["python3", str(synthetic_root / "scripts" / "m1_baseline_fixtures.py")],
        cwd=synthetic_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_m1_baseline_fixtures_script_succeeds_without_planning_dir(tmp_path) -> None:
    """Regression: script succeeds on a clean clone (no ``.planning/``).

    Simulates a fresh clone / CI runner where only
    ``library/distributed-audit/`` (tracked) exists. The script must
    fall back to ``library/`` as the audit source, mirror it into
    ``docs/distributed-audit/``, write the four fixture files, and exit 0.
    """
    repo = _build_synthetic_repo(tmp_path, with_planning=False, with_library=True)
    assert not (repo / ".planning" / "distributed-audit").exists()
    assert (repo / "library" / "distributed-audit").is_dir()

    code, stdout, stderr = _run_synthetic_script(repo)
    assert code == 0, f"non-zero exit; stderr={stderr!r}"

    # docs/distributed-audit/ must now exist and mirror library/distributed-audit/
    fake_docs = repo / "docs" / "distributed-audit"
    fake_library = repo / "library" / "distributed-audit"
    assert fake_docs.is_dir(), f"docs mirror missing; stdout={stdout!r}"
    library_names = {p.name for p in fake_library.iterdir() if p.is_file()}
    docs_names = {p.name for p in fake_docs.iterdir() if p.is_file()}
    assert library_names == docs_names, (
        f"only-in-library: {library_names - docs_names}, only-in-docs: {docs_names - library_names}"
    )
    for name in library_names:
        assert (fake_library / name).read_bytes() == (fake_docs / name).read_bytes(), f"drift: {name}"

    # The four canonical fixture files must also be present.
    for rel in (
        "tests/fixtures/v3_tasks.json",
        "tests/fixtures/v4_tasks.json",
        "tests/fixtures/baselines/events_payload_v4.3.1.json",
        "tests/fixtures/whilly_state-v4.3.json",
    ):
        assert (repo / rel).is_file(), f"missing fixture: {rel}"


def test_m1_baseline_fixtures_script_noop_when_no_audit_source(tmp_path) -> None:
    """Regression: script no-ops cleanly when neither audit source exists.

    Simulates an exotic environment where neither ``.planning/`` nor
    ``library/distributed-audit/`` has been populated yet. The script
    must NOT fail (exit 0), must emit a clear note on stderr, must NOT
    create ``docs/distributed-audit/``, but must still produce the four
    canonical fixture files.
    """
    repo = _build_synthetic_repo(tmp_path, with_planning=False, with_library=False)

    code, stdout, stderr = _run_synthetic_script(repo)
    assert code == 0, f"non-zero exit; stderr={stderr!r}"
    assert "neither" in stderr.lower() and "no-op" in stderr.lower(), (
        f"expected clear stderr note about empty audit source, got: {stderr!r}"
    )
    # docs/distributed-audit/ should NOT have been auto-created in this case.
    assert not (repo / "docs" / "distributed-audit").exists(), (
        "docs/distributed-audit/ should not be created when no source is reachable"
    )
    # Fixture writes are unrelated to the audit source and must still run.
    for rel in (
        "tests/fixtures/v3_tasks.json",
        "tests/fixtures/v4_tasks.json",
        "tests/fixtures/baselines/events_payload_v4.3.1.json",
        "tests/fixtures/whilly_state-v4.3.json",
    ):
        assert (repo / rel).is_file(), f"missing fixture: {rel}"


def test_m1_baseline_fixtures_script_prefers_planning_over_library(tmp_path) -> None:
    """Regression: when both sources exist, ``.planning/`` wins.

    Plants distinct sentinel content in ``.planning/distributed-audit/``
    vs ``library/distributed-audit/``. After running the script, the
    docs/ mirror must match ``.planning/`` (the higher-priority source).
    """
    repo = _build_synthetic_repo(tmp_path, with_planning=True, with_library=True)
    # Overwrite the planning copy with a sentinel so we can prove preference.
    sentinel_path = repo / ".planning" / "distributed-audit" / "current-state.md"
    assert sentinel_path.is_file()
    sentinel_path.write_bytes(b"# planning-source-sentinel\n")
    library_path = repo / "library" / "distributed-audit" / "current-state.md"
    assert library_path.read_bytes() != b"# planning-source-sentinel\n"

    code, _, stderr = _run_synthetic_script(repo)
    assert code == 0, f"non-zero exit; stderr={stderr!r}"

    docs_path = repo / "docs" / "distributed-audit" / "current-state.md"
    assert docs_path.is_file()
    assert docs_path.read_bytes() == b"# planning-source-sentinel\n", (
        "docs/ mirror should reflect .planning/ when both sources exist"
    )
