"""Unit tests for the claude_handoff backend.

Covers the file protocol (prompt/meta/result paths), result parsing across the
five ``status`` values, status-mapping into whilly's VALID_STATUSES, the sync
``run`` loop, the async ``run_async`` Popen shape, and the helper functions
used by the ``--handoff-*`` CLI commands.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from whilly.agents.claude_handoff import (
    ClaudeHandoffBackend,
    DEFAULT_HANDOFF_DIR,
    handoff_root,
    handoff_status_to_whilly,
    list_pending,
    task_dir_for,
    write_result,
)
from whilly.agents.base import COMPLETION_MARKER
from whilly.task_manager import VALID_STATUSES


@pytest.fixture(autouse=True)
def _scoped_handoff_dir(monkeypatch, tmp_path):
    """Every test operates under a tmp_path so no repo state bleeds in."""
    monkeypatch.setenv("WHILLY_HANDOFF_DIR", str(tmp_path / "handoff"))
    # Also force a tight timeout so tests can't hang.
    monkeypatch.setenv("WHILLY_HANDOFF_TIMEOUT", "3")
    yield


# ─── paths + env ───────────────────────────────────────────────────────────────


def test_handoff_root_honours_env(tmp_path):
    assert handoff_root() == Path(str(tmp_path / "handoff"))


def test_handoff_root_default_when_unset(monkeypatch):
    monkeypatch.delenv("WHILLY_HANDOFF_DIR", raising=False)
    assert handoff_root() == Path(DEFAULT_HANDOFF_DIR)


def test_task_dir_scrubs_path_separators():
    td = task_dir_for("GH/164")
    assert "GH_164" in str(td)
    assert "/" not in td.name


# ─── status mapping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("complete", "done"),
        ("failed", "failed"),
        ("blocked", "blocked"),
        ("human_loop", "human_loop"),
        ("partial", "done"),
        ("COMPLETE", "done"),  # case-insensitive
        ("unknown_state", "failed"),  # unknown → failed
        ("", "failed"),
    ],
)
def test_handoff_status_to_whilly_mapping(raw, expected):
    mapped = handoff_status_to_whilly(raw)
    assert mapped == expected
    # Sanity — mapped value must always be a real TaskManager status so
    # ``tm.mark_status([...], mapped)`` won't raise.
    assert mapped in VALID_STATUSES


def test_task_manager_accepts_new_statuses():
    # blocked + human_loop extend the frozenset so the orchestrator can set them.
    assert "blocked" in VALID_STATUSES
    assert "human_loop" in VALID_STATUSES


# ─── parse_output ──────────────────────────────────────────────────────────────


def test_parse_output_complete_appends_marker():
    backend = ClaudeHandoffBackend()
    raw = json.dumps({"status": "complete", "message": "implemented the thing"})
    text, usage = backend.parse_output(raw)
    assert "implemented the thing" in text
    assert COMPLETION_MARKER in text
    assert usage.cost_usd == 0.0


def test_parse_output_failed_no_marker():
    backend = ClaudeHandoffBackend()
    raw = json.dumps({"status": "failed", "message": "tests did not pass"})
    text, _usage = backend.parse_output(raw)
    assert "tests did not pass" in text
    assert COMPLETION_MARKER not in text


def test_parse_output_honours_usage_fields():
    backend = ClaudeHandoffBackend()
    raw = json.dumps(
        {
            "status": "complete",
            "message": "m",
            "duration_s": 12.5,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 500,
                "cost_usd": 0.02,
                "num_turns": 3,
            },
        }
    )
    _text, usage = backend.parse_output(raw)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 500
    assert usage.cost_usd == pytest.approx(0.02)
    assert usage.num_turns == 3
    assert usage.duration_ms == 12500


def test_parse_output_malformed_json_does_not_raise():
    backend = ClaudeHandoffBackend()
    text, usage = backend.parse_output("{ not json ]")
    assert text == "{ not json ]"
    assert usage.cost_usd == 0.0


def test_is_complete_flag():
    backend = ClaudeHandoffBackend()
    assert backend.is_complete(f"done\n{COMPLETION_MARKER}") is True
    assert backend.is_complete("just text") is False


# ─── write_result / list_pending ──────────────────────────────────────────────


def test_write_result_round_trips(tmp_path):
    # Simulate the "whilly dispatched a task" state: prompt exists.
    td = task_dir_for("GH-42")
    td.mkdir(parents=True)
    (td / "prompt.md").write_text("do stuff", encoding="utf-8")

    path = write_result(
        "GH-42",
        status="complete",
        message="did the stuff",
        cost_usd=0.05,
        num_turns=2,
        duration_s=7.5,
        input_tokens=10,
        output_tokens=20,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "complete"
    assert payload["message"] == "did the stuff"
    assert payload["duration_s"] == 7.5
    assert payload["usage"]["cost_usd"] == pytest.approx(0.05)
    assert payload["usage"]["duration_ms"] == 7500
    assert payload["usage"]["input_tokens"] == 10


def test_write_result_rejects_unknown_status():
    td = task_dir_for("GH-42")
    td.mkdir(parents=True)
    (td / "prompt.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown handoff status"):
        write_result("GH-42", status="explosion", message="")


def test_write_result_requires_dispatch():
    with pytest.raises(FileNotFoundError):
        write_result("NEVER-DISPATCHED", status="complete", message="")


def test_list_pending_ignores_tasks_with_results():
    td_pending = task_dir_for("GH-1")
    td_pending.mkdir(parents=True)
    (td_pending / "prompt.md").write_text("x", encoding="utf-8")
    (td_pending / "meta.json").write_text(
        json.dumps({"task_id": "GH-1", "started_at": "now", "plan_file": "plan.json"}),
        encoding="utf-8",
    )

    td_finished = task_dir_for("GH-2")
    td_finished.mkdir(parents=True)
    (td_finished / "prompt.md").write_text("y", encoding="utf-8")
    (td_finished / "result.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")

    rows = list_pending()
    task_ids = {r["task_id"] for r in rows}
    assert "GH-1" in task_ids
    assert "GH-2" not in task_ids


# ─── sync run (fast) ───────────────────────────────────────────────────────────


def test_run_returns_when_result_file_appears(monkeypatch):
    backend = ClaudeHandoffBackend()
    prompt = "id: GH-7\n\nbuild the thing"

    # Pre-create the directory + result so run() returns on its first check.
    td = task_dir_for("GH-7")
    td.mkdir(parents=True)
    (td / "result.json").write_text(
        json.dumps({"status": "complete", "message": "done"}),
        encoding="utf-8",
    )
    # Also allow run() to create its own prompt — but we wrote the result first,
    # so the loop exits almost immediately without waiting.
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # safety: don't actually sleep

    result = backend.run(prompt, timeout=5)
    assert result.is_complete
    assert result.exit_code == 0
    assert "done" in result.result_text


def test_run_times_out_when_no_result(monkeypatch):
    backend = ClaudeHandoffBackend()
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    result = backend.run("id: GH-timeout\n\nnothing", timeout=0)
    assert result.exit_code == 124
    assert result.is_complete is False
    assert "timed out" in result.result_text.lower()


# ─── async run — exercises the dispatch path with a stubbed Popen ─────────────


def test_run_async_writes_prompt_and_preamble(tmp_path, monkeypatch):
    """Dispatch should write prompt.md + meta.json + a log preamble before Popen spawns."""
    backend = ClaudeHandoffBackend()
    log_file = tmp_path / "agent.log"

    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, cwd=None, env=None):
            captured["cmd"] = cmd
            captured["env"] = env

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

        returncode = 0

    monkeypatch.setattr("whilly.agents.claude_handoff.subprocess.Popen", _FakePopen)

    proc = backend.run_async("id: GH-preamble\n\nbuild thing", log_file=log_file)

    td = task_dir_for("GH-preamble")
    assert (td / "prompt.md").is_file()
    assert (td / "meta.json").is_file()
    assert "build thing" in (td / "prompt.md").read_text(encoding="utf-8")

    # Preamble block is flushed synchronously, before the fake Popen runs.
    preamble = log_file.read_text(encoding="utf-8")
    assert "whilly handoff preamble" in preamble
    assert "GH-preamble" in preamble
    assert "result.json" in preamble

    # Popen gets the expected env var so its polling script can find the result.
    assert "WHILLY_HANDOFF_RESULT_PATH" in captured["env"]
    assert captured["env"]["WHILLY_HANDOFF_RESULT_PATH"].endswith("result.json")
    proc.wait()


def test_collect_result_from_file_reads_json_after_preamble(tmp_path):
    backend = ClaudeHandoffBackend()
    log_file = tmp_path / "agent.log"
    # Mimic what run_async writes + what the polling subprocess appends.
    log_file.write_text(
        "# whilly handoff preamble\n"
        "# task_id : GH-9\n"
        "# ---\n" + json.dumps({"status": "complete", "message": "all good", "usage": {"cost_usd": 0.01}}),
        encoding="utf-8",
    )
    result = backend.collect_result_from_file(log_file)
    assert result.is_complete
    assert "all good" in result.result_text
    assert result.usage.cost_usd == pytest.approx(0.01)


def test_collect_result_from_file_handles_failed_status(tmp_path):
    backend = ClaudeHandoffBackend()
    log_file = tmp_path / "agent.log"
    log_file.write_text(
        "# preamble\n# ---\n" + json.dumps({"status": "failed", "message": "could not parse spec"}),
        encoding="utf-8",
    )
    result = backend.collect_result_from_file(log_file)
    assert result.is_complete is False
    assert "could not parse spec" in result.result_text


# ─── backend registry ─────────────────────────────────────────────────────────


def test_backend_registered_under_claude_handoff():
    from whilly.agents import available_backends, get_backend

    assert "claude_handoff" in available_backends()
    backend = get_backend("claude_handoff")
    assert backend.name == "claude_handoff"
