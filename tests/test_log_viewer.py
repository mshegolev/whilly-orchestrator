"""Tests for the per-task log viewer + cleanup added in the v3 logging upgrade.

Two concerns kept after TASK-107 removed the legacy ``_log_event`` writer:

1. ``cleanup_old_logs`` deletes only what it should (per-task artifacts) and
   keeps the global ``whilly_events.jsonl`` + the rotating ``whilly.log``.
2. ``cmd_list`` discovers tasks from per-task event files (which are seeded
   directly by this test, not by the removed legacy emitter).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from whilly.log_viewer import cleanup_old_logs, cmd_list, discover_tasks


def _set_old(path: Path, days_old: int) -> None:
    cutoff = time.time() - days_old * 86400
    os.utime(path, (cutoff, cutoff))


def _write_event(log_dir: Path, event: str, **payload: object) -> None:
    """Inline replacement for the removed legacy ``_log_event`` helper.

    Writes the same JSONL shape the legacy emitter produced (global file
    plus per-task file when ``task_id`` is set) so the on-disk format
    pinned by :mod:`whilly.log_viewer` keeps a stable test fixture even
    after the v3 writer that produced it was removed in TASK-107.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "whilly_events.jsonl").open("a", encoding="utf-8").write(line)
    task_id = payload.get("task_id")
    if task_id:
        per_task = log_dir / "tasks"
        per_task.mkdir(parents=True, exist_ok=True)
        (per_task / f"{task_id}.events.jsonl").open("a", encoding="utf-8").write(line)


def test_cleanup_removes_only_old_per_task_artifacts(tmp_path: Path) -> None:
    log_dir = tmp_path / "whilly_logs"
    log_dir.mkdir()
    tasks = log_dir / "tasks"
    tasks.mkdir()

    old_log = log_dir / "T-1.log"
    old_log.write_text("old stdout")
    _set_old(old_log, 30)

    fresh_log = log_dir / "T-2.log"
    fresh_log.write_text("fresh stdout")

    old_prompt = log_dir / "T-1_prompt.txt"
    old_prompt.write_text("prompt")
    _set_old(old_prompt, 30)

    old_events = tasks / "T-1.events.jsonl"
    old_events.write_text('{"event": "x"}\n')
    _set_old(old_events, 30)

    # Files that MUST survive even when old:
    global_jsonl = log_dir / "whilly_events.jsonl"
    global_jsonl.write_text('{"event": "plan_start"}\n')
    _set_old(global_jsonl, 30)

    whilly_log = log_dir / "whilly.log"
    whilly_log.write_text("Whilly bootstrap")
    _set_old(whilly_log, 30)

    rotated = log_dir / "whilly.log.1"
    rotated.write_text("rotated backup")
    _set_old(rotated, 30)

    removed = cleanup_old_logs(log_dir, ttl_days=14)

    assert removed == 3
    assert not old_log.exists()
    assert not old_prompt.exists()
    assert not old_events.exists()
    assert fresh_log.exists()
    assert global_jsonl.exists()  # never expires via this path
    assert whilly_log.exists()  # RotatingFileHandler owns this
    assert rotated.exists()


def test_cleanup_disabled_when_ttl_zero(tmp_path: Path) -> None:
    log_dir = tmp_path / "whilly_logs"
    log_dir.mkdir()
    old = log_dir / "T-1.log"
    old.write_text("data")
    _set_old(old, 100)

    assert cleanup_old_logs(log_dir, ttl_days=0) == 0
    assert old.exists()


def test_discover_tasks_reads_per_task_events(tmp_path: Path) -> None:
    log_dir = tmp_path / "whilly_logs"
    log_dir.mkdir()
    _write_event(log_dir, "task_complete", task_id="T-7", duration_s=2.0, cost_usd=0.05)

    summaries = discover_tasks(log_dir)

    assert len(summaries) == 1
    s = summaries[0]
    assert s.task_id == "T-7"
    assert s.status == "done"
    assert s.duration_s == 2.0
    assert s.cost_usd == 0.05
    assert s.has_events is True


def test_cmd_list_with_no_logs_is_friendly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "whilly_logs"
    empty.mkdir()
    rc = cmd_list(empty)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No task logs found" in out
