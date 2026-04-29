"""Unit tests for whilly.prd_generator extraction (TASK-104a-1).

Covers the new :func:`generate_tasks_dict` entry point plus the
preserved behaviour of :func:`generate_tasks` after the
``_build_tasks_payload`` extraction.

Why these tests exist
---------------------
``whilly/prd_generator.py`` had zero unit tests before TASK-104a-1.
Refactoring an untested module is the usual recipe for silent
regressions — so the safe move is to add tests *as part of the same
commit* as the extraction. The tests pin both:

* ``generate_tasks_dict`` returns the right shape with ``plan_id``
  stamped (the new v4 contract — see PRD-v41-prd-wizard-port.md FR-4).
* ``generate_tasks`` still writes ``<slug>_tasks.json`` and returns the
  ``Path`` (the v3 contract that ``whilly --prd-wizard`` legacy flow
  depends on — must not regress per PRD NFR-4 / SC-4).

Both paths share the extracted ``_build_tasks_payload`` helper, so the
JSON-parsing / fallback / default-fill logic is exercised through
either entry point.

All tests mock :func:`whilly.prd_generator._call_claude` so the suite
runs without ``CLAUDE_BIN`` on PATH and in <50ms total — no subprocess
spawn, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from whilly.prd_generator import _build_tasks_payload, generate_tasks, generate_tasks_dict


_FAKE_CLAUDE_RESPONSE = json.dumps(
    {
        "project": "Test project",
        "tasks": [
            {
                "id": "TASK-001",
                "title": "First task",
                "description": "Do the first thing",
                "priority": "high",
            },
            {
                "title": "Second task",
                "description": "Do the second thing",
                "priority": "medium",
            },
        ],
    }
)


@pytest.fixture
def prd_file(tmp_path: Path) -> Path:
    """Materialise a minimal PRD markdown file for the helper to read."""
    p = tmp_path / "PRD-test-project.md"
    p.write_text(
        "# PRD: Test\n\n## Goals\n\nDo the test thing.\n",
        encoding="utf-8",
    )
    return p


# ─── _build_tasks_payload ─────────────────────────────────────────────────


def test_build_tasks_payload_returns_validated_dict(prd_file: Path) -> None:
    """Helper returns a dict matching what Claude responded, with task defaults filled."""
    with patch("whilly.prd_generator._call_claude", return_value=_FAKE_CLAUDE_RESPONSE):
        data = _build_tasks_payload(prd_file, model="test-model")

    assert data["project"] == "Test project"
    assert len(data["tasks"]) == 2

    # Every task gets defaults applied — even the ones the LLM didn't bother filling.
    for task in data["tasks"]:
        assert task["status"] == "pending"
        assert task["dependencies"] == []
        assert task["key_files"] == []
        assert task["acceptance_criteria"] == []
        assert task["test_steps"] == []
        assert "id" in task

    # Auto-generated id for the second task (LLM omitted it).
    assert data["tasks"][1]["id"] == "TASK-002"


def test_build_tasks_payload_strips_markdown_fences(prd_file: Path) -> None:
    """Claude often wraps JSON in ```json ... ``` fences; helper strips them."""
    fenced = "```json\n" + _FAKE_CLAUDE_RESPONSE + "\n```"
    with patch("whilly.prd_generator._call_claude", return_value=fenced):
        data = _build_tasks_payload(prd_file, model="test-model")
    assert data["project"] == "Test project"


def test_build_tasks_payload_raises_on_missing_prd(tmp_path: Path) -> None:
    """Missing PRD file → FileNotFoundError before any Claude call."""
    missing = tmp_path / "nope.md"
    with patch("whilly.prd_generator._call_claude") as mock_claude:
        with pytest.raises(FileNotFoundError, match="PRD not found"):
            _build_tasks_payload(missing, model="test-model")
    mock_claude.assert_not_called()


def test_build_tasks_payload_raises_on_empty_response(prd_file: Path) -> None:
    """Empty Claude response → RuntimeError, not a confusing JSONDecodeError."""
    with patch("whilly.prd_generator._call_claude", return_value=""):
        with pytest.raises(RuntimeError, match="empty response"):
            _build_tasks_payload(prd_file, model="test-model")


def test_build_tasks_payload_raises_on_no_tasks(prd_file: Path) -> None:
    """Valid JSON but no tasks → RuntimeError."""
    empty_plan = json.dumps({"project": "X", "tasks": []})
    with patch("whilly.prd_generator._call_claude", return_value=empty_plan):
        with pytest.raises(RuntimeError, match="No tasks generated"):
            _build_tasks_payload(prd_file, model="test-model")


def test_build_tasks_payload_raw_dump_on_invalid_json(prd_file: Path, tmp_path: Path) -> None:
    """Invalid JSON + raw_dump_path → forensics file written, RuntimeError raised."""
    raw_path = tmp_path / "bad.raw.txt"
    with patch("whilly.prd_generator._call_claude", return_value="this is { definitely not json"):
        with pytest.raises(RuntimeError, match="Invalid JSON"):
            _build_tasks_payload(prd_file, model="test-model", raw_dump_path=raw_path)
    assert raw_path.exists(), "raw forensics file should have been written"
    assert "this is { definitely not json" in raw_path.read_text(encoding="utf-8")


# ─── generate_tasks (v3 file-based flow — must not regress) ───────────────


def test_generate_tasks_writes_json_file(prd_file: Path, tmp_path: Path) -> None:
    """v3 contract: writes <slug>_tasks.json next to PRD, returns Path."""
    out_dir = tmp_path / ".planning"
    with patch("whilly.prd_generator._call_claude", return_value=_FAKE_CLAUDE_RESPONSE):
        out_path = generate_tasks(prd_file, output_dir=str(out_dir), model="test-model")

    assert out_path.exists()
    assert out_path.parent == out_dir
    assert out_path.suffix == ".json"

    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["project"] == "Test project"
    assert len(written["tasks"]) == 2


def test_generate_tasks_filename_strips_prd_prefix(prd_file: Path, tmp_path: Path) -> None:
    """v3 derives the output basename from the PRD's stem, stripping 'PRD-' prefix."""
    out_dir = tmp_path / ".planning"
    with patch("whilly.prd_generator._call_claude", return_value=_FAKE_CLAUDE_RESPONSE):
        out_path = generate_tasks(prd_file, output_dir=str(out_dir), model="test-model")

    # PRD-test-project.md → test-project_tasks.json
    assert out_path.name == "test-project_tasks.json"


# ─── generate_tasks_dict (new v4 entry point — TASK-104a-1) ───────────────


def test_generate_tasks_dict_returns_dict_with_plan_id(prd_file: Path) -> None:
    """v4 contract: returns dict in memory, plan_id is stamped explicitly."""
    with patch("whilly.prd_generator._call_claude", return_value=_FAKE_CLAUDE_RESPONSE):
        data = generate_tasks_dict(prd_file, plan_id="my-plan", model="test-model")

    assert isinstance(data, dict)
    assert data["plan_id"] == "my-plan"
    assert data["project"] == "Test project"
    assert len(data["tasks"]) == 2

    # Same defaults applied as in generate_tasks.
    for task in data["tasks"]:
        assert task["status"] == "pending"
        assert "id" in task


def test_generate_tasks_dict_does_not_write_to_disk(
    prd_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v4 contract: no file written anywhere — pure in-memory return.

    Sentinel: snapshot the contents of tmp_path before and after; nothing
    new should appear (PRD itself is in there but was created by the
    fixture before the call).
    """
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))

    with patch("whilly.prd_generator._call_claude", return_value=_FAKE_CLAUDE_RESPONSE):
        generate_tasks_dict(prd_file, plan_id="my-plan", model="test-model")

    after = set(tmp_path.rglob("*"))
    new_files = after - before
    assert not new_files, f"generate_tasks_dict should not write any files; found new: {new_files}"


def test_generate_tasks_dict_overrides_plan_id_from_claude(prd_file: Path) -> None:
    """If Claude happens to set plan_id in its response, the explicit param wins.

    Slug ownership lives in the CLI (FR-3 of the PRD): Claude shouldn't
    be in the loop on naming. So even if a future prompt change has
    Claude include a plan_id, the caller's value must override it.
    """
    response_with_plan_id = json.dumps(
        {
            "project": "Test project",
            "plan_id": "claude-picked-this",
            "tasks": [{"id": "TASK-001", "title": "X", "description": "x", "priority": "high"}],
        }
    )
    with patch("whilly.prd_generator._call_claude", return_value=response_with_plan_id):
        data = generate_tasks_dict(prd_file, plan_id="caller-picked", model="test-model")

    assert data["plan_id"] == "caller-picked"


def test_generate_tasks_dict_propagates_runtime_errors(prd_file: Path) -> None:
    """Errors from _build_tasks_payload reach the caller unchanged."""
    with patch("whilly.prd_generator._call_claude", return_value=""):
        with pytest.raises(RuntimeError, match="empty response"):
            generate_tasks_dict(prd_file, plan_id="my-plan", model="test-model")
