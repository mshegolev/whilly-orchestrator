"""Per-task log viewer + age-based cleanup for ``whilly_logs/``.

Three CLI subcommands wired via ``whilly logs ...`` in :mod:`whilly.cli`:

* ``whilly logs --list``                - table of tasks from the last run(s)
* ``whilly logs <task_id>``             - prompt + events + stdout in one view
* ``whilly logs --tail <task_id>``      - live follow (events + stdout)

Plus :func:`cleanup_old_logs` that ``run_plan`` calls at startup.

Dependencies: only stdlib + ANSI helpers from :mod:`whilly.cli`. No Rich,
no watchdog — keeps the viewer usable on a stripped-down system.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ANSI helpers mirror cli.py:75-86 so output stays consistent across the project.
R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
GR = "\033[32m"
YL = "\033[33m"
CY = "\033[36m"
RD = "\033[31m"
MG = "\033[35m"
WH = "\033[97m"


# ── Cleanup ───────────────────────────────────────────────────────────────────


def cleanup_old_logs(log_dir: Path, ttl_days: int) -> int:
    """Remove agent logs older than ``ttl_days`` days. Returns count removed.

    Touches: ``{task_id}.log``, ``{task_id}_prompt.txt``, ``seq_iter*.log``,
    everything under ``tasks/`` (per-task events + http_trace.jsonl).

    Spares: ``whilly_events.jsonl`` (global timeline, never expires here),
    ``whilly.log*`` (already managed by RotatingFileHandler in cli.main).

    Disabled when ``ttl_days <= 0``.
    """
    if ttl_days <= 0 or not log_dir.is_dir():
        return 0

    cutoff = time.time() - ttl_days * 86400
    removed = 0

    candidates: list[Path] = []
    candidates += list(log_dir.glob("*.log"))
    candidates += list(log_dir.glob("*_prompt.txt"))
    tasks_dir = log_dir / "tasks"
    if tasks_dir.is_dir():
        candidates += [p for p in tasks_dir.iterdir() if p.is_file()]

    for path in candidates:
        # Skip whilly's own logger file and its rotated backups (whilly.log, .log.1, .log.2…)
        name = path.name
        if name == "whilly.log" or name.startswith("whilly.log."):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass

    return removed


# ── Discovery ─────────────────────────────────────────────────────────────────


@dataclass
class TaskSummary:
    task_id: str
    status: str = "unknown"
    duration_s: float = 0.0
    cost_usd: float = 0.0
    last_event_ts: str = ""
    has_log: bool = False
    has_prompt: bool = False
    has_events: bool = False


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def discover_tasks(log_dir: Path) -> list[TaskSummary]:
    """Find every task that has at least one artifact in ``log_dir``."""
    if not log_dir.is_dir():
        return []

    summaries: dict[str, TaskSummary] = {}

    # Per-task event files (preferred — richest source).
    tasks_dir = log_dir / "tasks"
    if tasks_dir.is_dir():
        for ev_file in tasks_dir.glob("*.events.jsonl"):
            tid = ev_file.name[: -len(".events.jsonl")]
            s = summaries.setdefault(tid, TaskSummary(task_id=tid))
            s.has_events = True
            for entry in _read_jsonl(ev_file):
                _apply_event(s, entry)

    # Fallback: scan global jsonl for tasks that don't yet have a per-task file
    # (e.g. logs from before this upgrade rolled out).
    global_jsonl = log_dir / "whilly_events.jsonl"
    if global_jsonl.is_file():
        for entry in _read_jsonl(global_jsonl):
            tid = entry.get("task_id")
            if not tid:
                continue
            s = summaries.setdefault(tid, TaskSummary(task_id=tid))
            _apply_event(s, entry)

    # Mark which artifacts exist on disk.
    for s in summaries.values():
        s.has_log = (log_dir / f"{s.task_id}.log").is_file()
        s.has_prompt = (log_dir / f"{s.task_id}_prompt.txt").is_file()

    # Also surface tasks that only have a .log/_prompt without events.
    for log in log_dir.glob("*.log"):
        if log.name.startswith("whilly.log") or log.name.startswith("seq_iter"):
            continue
        tid = log.stem
        s = summaries.setdefault(tid, TaskSummary(task_id=tid))
        s.has_log = True

    return sorted(summaries.values(), key=lambda s: s.last_event_ts or "")


def _apply_event(s: TaskSummary, entry: dict) -> None:
    ts = entry.get("ts") or ""
    if ts > s.last_event_ts:
        s.last_event_ts = ts
    event = entry.get("event") or ""
    if event == "task_complete":
        s.status = "done"
        s.duration_s = float(entry.get("duration_s", 0) or 0)
        s.cost_usd = float(entry.get("cost_usd", 0) or 0)
    elif event == "task_skipped":
        s.status = "skipped"
    elif event in ("task_start", "batch_start"):
        if s.status == "unknown":
            s.status = "started"


# ── Subcommands ───────────────────────────────────────────────────────────────


def cmd_list(log_dir: Path) -> int:
    """Print a table of tasks discovered in ``log_dir``. Returns exit code."""
    summaries = discover_tasks(log_dir)
    if not summaries:
        print(f"{YL}No task logs found in {log_dir}{R}")
        return 0

    color = sys.stdout.isatty()

    def c(text: str, code: str) -> str:
        return f"{code}{text}{R}" if color else text

    headers = ("TASK_ID", "STATUS", "DURATION", "COST", "LAST_EVENT")
    rows = []
    for s in summaries:
        rows.append(
            (
                s.task_id,
                s.status,
                f"{s.duration_s:.1f}s" if s.duration_s else "-",
                f"${s.cost_usd:.4f}" if s.cost_usd else "-",
                s.last_event_ts[:19] if s.last_event_ts else "-",
            )
        )

    widths = [max(len(str(row[i])) for row in (headers, *rows)) for i in range(len(headers))]

    def fmt_row(row: tuple, header: bool = False) -> str:
        cells = []
        for i, value in enumerate(row):
            text = str(value).ljust(widths[i])
            if header:
                text = c(text, B + WH)
            elif i == 1:  # status column — colorize
                if value == "done":
                    text = c(text, GR)
                elif value == "skipped":
                    text = c(text, YL)
                elif value in ("failed", "unknown"):
                    text = c(text, RD)
            cells.append(text)
        return "  ".join(cells)

    print(fmt_row(headers, header=True))
    print(c("-" * (sum(widths) + 2 * (len(widths) - 1)), D))
    for row in rows:
        print(fmt_row(row))

    return 0


def cmd_show(log_dir: Path, task_id: str) -> int:
    """Print prompt + events timeline + stdout for one task."""
    color = sys.stdout.isatty()

    def c(text: str, code: str) -> str:
        return f"{code}{text}{R}" if color else text

    prompt_file = log_dir / f"{task_id}_prompt.txt"
    events_file = log_dir / "tasks" / f"{task_id}.events.jsonl"
    log_file = log_dir / f"{task_id}.log"

    found_any = False

    if prompt_file.is_file():
        found_any = True
        print(c(f"━━━ PROMPT ({prompt_file.name}) ━━━", B + CY))
        print(prompt_file.read_text(encoding="utf-8", errors="replace"))
        print()

    if events_file.is_file():
        found_any = True
        print(c(f"━━━ EVENTS ({events_file.relative_to(log_dir)}) ━━━", B + MG))
        for entry in _read_jsonl(events_file):
            ts = entry.get("ts", "")[:19]
            ev = entry.get("event", "?")
            extras = {k: v for k, v in entry.items() if k not in ("ts", "event", "task_id")}
            extras_str = " ".join(f"{k}={v!r}" for k, v in extras.items())
            ev_color = GR if ev == "task_complete" else (YL if "skip" in ev else CY)
            print(f"{c(ts, D)}  {c(ev, ev_color)}  {extras_str}")
        print()
    else:
        # Fallback: filter global jsonl.
        global_jsonl = log_dir / "whilly_events.jsonl"
        if global_jsonl.is_file():
            entries = [e for e in _read_jsonl(global_jsonl) if e.get("task_id") == task_id]
            if entries:
                found_any = True
                print(c(f"━━━ EVENTS (filtered from {global_jsonl.name}) ━━━", B + MG))
                for entry in entries:
                    ts = entry.get("ts", "")[:19]
                    ev = entry.get("event", "?")
                    extras = {k: v for k, v in entry.items() if k not in ("ts", "event", "task_id")}
                    extras_str = " ".join(f"{k}={v!r}" for k, v in extras.items())
                    print(f"{c(ts, D)}  {c(ev, CY)}  {extras_str}")
                print()

    if log_file.is_file():
        found_any = True
        print(c(f"━━━ STDOUT ({log_file.name}) ━━━", B + GR))
        print(log_file.read_text(encoding="utf-8", errors="replace"))

    if not found_any:
        print(f"{RD}No artifacts found for task {task_id!r} in {log_dir}{R}")
        print(f"{D}Try `whilly logs --list` to see available task IDs.{R}")
        return 1
    return 0


def cmd_tail(log_dir: Path, task_id: str, poll_interval: float = 0.5) -> int:
    """Live follow events + stdout for a task. Ctrl-C to exit."""
    events_file = log_dir / "tasks" / f"{task_id}.events.jsonl"
    log_file = log_dir / f"{task_id}.log"
    color = sys.stdout.isatty()

    def c(text: str, code: str) -> str:
        return f"{code}{text}{R}" if color else text

    if not events_file.is_file() and not log_file.is_file():
        print(f"{RD}No log files yet for {task_id!r}. Will wait...{R}")

    print(c(f"Following task {task_id!r} (Ctrl-C to exit)", B + CY))
    pos_events = 0
    pos_log = 0

    try:
        while True:
            if events_file.is_file():
                with open(events_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos_events)
                    for raw in f:
                        if not raw.strip():
                            continue
                        try:
                            entry = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        ts = entry.get("ts", "")[:19]
                        ev = entry.get("event", "?")
                        extras = {k: v for k, v in entry.items() if k not in ("ts", "event", "task_id")}
                        extras_str = " ".join(f"{k}={v!r}" for k, v in extras.items())
                        ev_color = GR if ev == "task_complete" else (YL if "skip" in ev else CY)
                        print(f"{c('[ev]', MG)} {c(ts, D)} {c(ev, ev_color)} {extras_str}")
                    pos_events = f.tell()

            if log_file.is_file():
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos_log)
                    for raw in f:
                        sys.stdout.write(c("[out] ", D) + raw)
                    pos_log = f.tell()

            sys.stdout.flush()
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print()  # newline after ^C
        return 0


# ── Entry point dispatched from cli.main ──────────────────────────────────────


def run_logs_command(args: list[str], log_dir: Path) -> int:
    """Dispatch ``whilly logs ...`` arguments. ``args`` excludes the literal 'logs'."""
    if "--list" in args:
        return cmd_list(log_dir)

    tail = "--tail" in args or "-f" in args
    positional = [a for a in args if not a.startswith("-")]
    task_id = positional[0] if positional else None

    if not task_id:
        print(f"{YL}Usage:{R}")
        print("  whilly logs --list")
        print("  whilly logs <task-id>")
        print("  whilly logs --tail <task-id>      # live follow (also -f)")
        return 2

    if tail:
        return cmd_tail(log_dir, task_id)
    return cmd_show(log_dir, task_id)


__all__ = [
    "cleanup_old_logs",
    "cmd_list",
    "cmd_show",
    "cmd_tail",
    "discover_tasks",
    "run_logs_command",
    "TaskSummary",
]


# Used by cli.main when no LOG_DIR is in env yet.
DEFAULT_LOG_DIR = "whilly_logs"


def resolve_log_dir(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("WHILLY_LOG_DIR") or DEFAULT_LOG_DIR).resolve()
