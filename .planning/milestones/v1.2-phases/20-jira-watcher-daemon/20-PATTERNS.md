# Phase 20: Jira Watcher Daemon - Pattern Map

**Mapped:** 2026-06-12
**Files analyzed:** 4 new/modified files
**Analogs found:** 4 / 4

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `whilly/cli/jira_watch_loop.py` | service/utility (daemon loop) | event-driven, request-response | `whilly/cli/jira.py` (`_run_jira_smoke`) + `whilly/state_store.py` | role-match (freshest CLI pattern) |
| `whilly/cli/jira.py` | controller (CLI router) | request-response | `whilly/cli/jira.py` (self — `run_jira_command` action dispatch block) | exact |
| `tests/unit/cli/test_jira_watch_loop.py` | test | request-response | `tests/unit/cli/test_jira_smoke.py` + `tests/unit/test_worker_shutdown.py` | role-match |
| `docs/Whilly-Usage.md` | docs | — | existing watch section pattern in `docs/Whilly-Usage.md` | docs-only |

---

## Pattern Assignments

### `whilly/cli/jira_watch_loop.py` (service/utility, event-driven)

**Primary analog:** `whilly/cli/jira.py` (lines 715–930) — `_run_jira_smoke` + `_persist_smoke_event`
**Secondary analog:** `whilly/state_store.py` (lines 22–63) — atomic write

---

#### Imports pattern

Copy the import block shape from `whilly/cli/jira.py` lines 16–43, with these differences:
- Replace `argparse`-heavy imports with just `argparse`, `signal`, `threading`, `time`
- Add `import os`, `import tempfile` (both used in atomic status write)
- Keep `asyncio`, `json`, `logging`, `sys`, `pathlib.Path`
- No import from `whilly.cli.jira` — avoids circular import (see Pitfall 5 in RESEARCH.md)

```python
# Target import block for whilly/cli/jira_watch_loop.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import MutableMapping

from whilly.jira_watch import (
    JiraWorkSnapshot,
    collect_jira_work_snapshot,
    persist_jira_work_snapshot,
)
from whilly.llm_ops import _log_dir
from whilly.pause_control import PauseControl

log = logging.getLogger(__name__)

_BACKOFF_SEQUENCE = (5, 10, 20, 40, 60)
```

---

#### Credential gate pattern

Copy from `whilly/cli/jira.py` lines 746–762 (`_run_jira_smoke` credential gate block):

```python
# whilly/cli/jira.py lines 746-762
effective_config_loader = config_loader if config_loader is not None else _load_config
effective_config_reader = config_reader if config_reader is not None else _read_jira_config_section
effective_env: MutableMapping[str, str] = environ if environ is not None else os.environ
effective_config_loader()
config_rc = _ensure_jira_config(
    args,
    config_reader=effective_config_reader,
    env=effective_env,
    prompt=prompt or input,
    secret_prompt=secret_prompt or getpass.getpass,
    browser_opener=browser_opener or webbrowser.open,
    stdin_isatty=stdin_isatty or sys.stdin.isatty,
    command_label="whilly jira watch",
)
if config_rc != EXIT_OK:
    return EXIT_CONFIG_MISSING
```

**Critical:** `_ensure_jira_config` (jira.py line 1259) accesses `args.interactive_config`
directly — the watch subparser MUST declare `--interactive-config` / `--no-interactive-config`
flags, identical to the smoke subparser (jira.py lines 308–318):

```python
# whilly/cli/jira.py lines 308-318 — copy verbatim to watch subparser
p_watch.add_argument(
    "--interactive-config",
    action="store_true",
    help="Prompt for missing Jira settings before running.",
)
p_watch.add_argument(
    "--no-interactive-config",
    action="store_true",
    help="Never prompt for missing Jira settings; print setup instructions instead.",
)
```

---

#### Signal handler pattern (sync equivalent)

Reference: `whilly/worker/main.py` lines 171–252 (asyncio version — adapt to sync).
Use `signal.signal()` + `threading.Event` instead of `loop.add_signal_handler()` + `asyncio.Event`
because the watch loop is synchronous.

```python
# Sync equivalent of whilly/worker/main.py lines 171-252
# _SHUTDOWN_SIGNALS pattern: lines 108-110
import signal
import threading

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _install_watch_signal_handlers(stop: threading.Event) -> None:
    """Install SIGTERM/SIGINT handlers that set stop (sync equivalent of worker/main.py).

    Must not be called in tests (pass install_signal_handlers=False).
    """
    def _handler(signum: int, frame: object) -> None:
        log.info("received signal %d; requesting graceful shutdown", signum)
        stop.set()

    for sig in _SHUTDOWN_SIGNALS:
        signal.signal(sig, _handler)
```

The `install_signal_handlers=False` test seam mirrors `run_worker`'s identical param
(worker/main.py line 416 usage in tests: `install_signal_handlers=False`).

---

#### Interruptible sleep pattern

**Anti-pattern to avoid:** `time.sleep(interval)` — used in `whilly/github_projects.py`
line 722, not signal-responsive.

**Correct pattern** (derived from `asyncio.wait_for(stop.wait(), timeout=interval)` in
`worker/main.py` lines 162–168, translated to sync):

```python
# threading.Event.wait() returns True if event set (stop), False on timeout
def _interruptible_sleep(stop: threading.Event, seconds: float) -> bool:
    """Sleep for `seconds` or until stop is set.

    Returns True if stop was set (caller should exit loop),
    False if timeout elapsed normally (caller continues).
    """
    return stop.wait(timeout=seconds)
```

Used in the loop body:
```python
while not stop_event.is_set():
    if _interruptible_sleep(stop_event, interval + backoff_seconds):
        break   # stop was set — exit cleanly
    _run_one_cycle(...)
```

---

#### Atomic status file write pattern

Copy verbatim from `whilly/state_store.py` lines 36–63 (`StateStore.save`):

```python
# whilly/state_store.py lines 36-63 — atomic write (copy pattern)
def _write_status(status: dict[str, Any], status_path: Path) -> None:
    """Atomic overwrite of the watch status file."""
    d = status_path.parent
    d.mkdir(parents=True, exist_ok=True)
    content = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(d), suffix=".tmp", prefix=".jira-watch-status-"
    )
    closed = False
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        closed = True
        os.replace(tmp_path, status_path)
    except BaseException:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
```

**Note:** `state_store.py` line 50 uses `dir_path = self.state_file.parent or Path(".")` —
copy this defensive fallback too.

---

#### Best-effort DB persist pattern

Copy verbatim from `whilly/cli/jira.py` lines 863–882 (`_run_jira_smoke` best-effort block)
and lines 904–918 (`_persist_smoke_event`):

```python
# whilly/cli/jira.py lines 904-918 — persist helper (copy shape)
async def _persist_watch_event(
    *, dsn: str, issue_key: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Append a watch audit event to Postgres (best-effort)."""
    from whilly.adapters.db import close_pool, create_pool
    from whilly.adapters.db.repository import TaskRepository

    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        await repo.append_jira_work_event(
            issue_key=issue_key,
            event_type=event_type,
            payload=payload,
        )
    finally:
        await close_pool(pool)
```

Caller pattern (copy from jira.py lines 863–882):

```python
# whilly/cli/jira.py lines 863-882 — best-effort call site (copy shape)
dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()
if dsn:
    try:
        asyncio.run(_persist_watch_event(
            dsn=dsn, issue_key=issue_key,
            event_type="watch.cycle", payload=cycle_payload,
        ))
    except Exception as exc:  # noqa: BLE001 — best-effort must not kill watcher
        print(
            f"whilly jira watch: persist failed ({exc.__class__.__name__}) — "
            "best-effort, check WHILLY_DATABASE_URL connectivity.",
            file=sys.stderr,
        )
```

---

#### PauseControl gate pattern

Copy from `whilly/pause_control.py` lines 32–43:

```python
# whilly/pause_control.py lines 32-43
pc = PauseControl()  # reads .whilly_pause in cwd
if pc.is_paused():
    info = pc.get_pause_info() or {}
    reason = info.get("reason", "unknown")
    log.info("whilly jira watch: global pause active (%s); skipping dispatch", reason)
    # record watch.paused audit event (best-effort), then continue loop
```

---

#### Readiness gate pattern

Copy from `whilly/cli/jira.py` lines 622–631 (`_run_intake` run path):

```python
# whilly/cli/jira.py lines 622-631
readiness = _read_jira_work_readiness(plan_path)
if (
    readiness
    and readiness.get("verdict") != "ready_for_testing"
    and not bool(args.allow_unready_run)
):
    print(
        "whilly jira watch: readiness gate failed; "
        f"verdict={readiness.get('verdict')} "
        f"missing={','.join(readiness.get('missing_context') or [])}. "
        "Skipping dispatch until ready.",
        file=sys.stderr,
    )
    # record watch.block audit event (best-effort), skip dispatch, continue loop
```

`_read_jira_work_readiness` (jira.py lines 1138–1144) reads `data["jira_work"]["readiness"]`
from plan JSON on disk — no live probe, no Jira call.

---

#### `_run_jira_watch` function signature

Mirror `_run_jira_smoke` (jira.py lines 715–726) with watch-specific params:

```python
def _run_jira_watch(
    args: argparse.Namespace,
    *,
    snapshot_collector: SnapshotCollector,
    config_loader: ConfigLoader | None,
    config_reader: ConfigReader | None,
    environ: MutableMapping[str, str] | None,
    prompt: Prompt | None,
    secret_prompt: Prompt | None,
    browser_opener: BrowserOpener | None,
    stdin_isatty: IsATTY | None,
    stop_event: threading.Event | None = None,       # injectable for tests
    install_signal_handlers: bool = True,            # False in tests
) -> int:
```

Type aliases (`SnapshotCollector`, `ConfigLoader`, etc.) come from `whilly/cli/jira.py`
lines 49–58 — import them in `jira_watch_loop.py` from `whilly.cli.jira` is NOT safe (circular).
Redefine locally or use `Callable[..., JiraWorkSnapshot]` inline.

---

### `whilly/cli/jira.py` (controller, request-response — modification only)

**Analog:** Self — existing `run_jira_command` dispatch block (lines 364–448).

#### ACTION routing pattern to extend (lines 385–448)

```python
# whilly/cli/jira.py lines 419-432 — copy this dispatch shape for watch/watch-status
if args.action == "poll":
    return _run_poll(args, snapshot_collector=snapshot_collector or collect_jira_work_snapshot)
if args.action == "smoke":
    return _run_jira_smoke(
        args,
        snapshot_collector=snapshot_collector or collect_jira_work_snapshot,
        config_loader=config_loader,
        config_reader=config_reader,
        environ=environ,
        prompt=prompt,
        secret_prompt=secret_prompt,
        browser_opener=browser_opener,
        stdin_isatty=stdin_isatty,
    )
```

New watch dispatch follows this exact shape — lazy-import from `jira_watch_loop` to avoid
circular imports (same pattern as `tui` action: jira.py lines 434–436):

```python
# whilly/cli/jira.py lines 434-436 — lazy import pattern to replicate
if args.action == "tui":
    from whilly.cli.jira_tui import run_jira_tui_command
    return run_jira_tui_command(args, ...)
```

#### Subparser registration pattern (lines 287–318)

The `p_poll` and `p_smoke` subparser blocks (jira.py lines 287–318) show the pattern.
Add `p_watch` and `p_watch_status` in `build_jira_parser()` following this shape:

```python
# whilly/cli/jira.py lines 287-295 — poll subparser (copy shape for watch)
p_poll = sub.add_parser(
    "poll",
    help="Run one Jira refresh cycle: issue, comments, changelog, remote links, and repo hints.",
)
p_poll.add_argument("jira_ref", help="Jira key or browse URL.")
p_poll.add_argument("--timeout", type=int, default=15, help="...")
p_poll.add_argument("--plan-id", default="", help="...")
p_poll.add_argument("--persist", action="store_true", help="...")
p_poll.add_argument("--json", action="store_true", help="...")
```

---

### `tests/unit/cli/test_jira_watch_loop.py` (test, request-response)

**Primary analog:** `tests/unit/cli/test_jira_smoke.py` lines 1–65 — `_jira_env()`, `run_jira_command` call shape, `monkeypatch.setenv("WHILLY_LOG_DIR", ...)`.
**Secondary analog:** `tests/unit/test_worker_shutdown.py` lines 371–426 — `install_signal_handlers=False`, injectable `stop` event.
**Tertiary analog:** `tests/unit/test_jira_watch.py` lines 108–119 — `_FakeRepo` inject pattern.

---

#### Test env helper pattern

Copy from `tests/unit/cli/test_jira_smoke.py` lines 18–24:

```python
# tests/unit/cli/test_jira_smoke.py lines 18-24
def _jira_env() -> dict[str, str]:
    return {
        "JIRA_SERVER_URL": "https://company.atlassian.net",
        "JIRA_USERNAME": "dev@example.com",
        "JIRA_API_TOKEN": "jira-token",
    }
```

---

#### Fake snapshot builder pattern

Copy from `tests/unit/cli/test_jira_smoke.py` lines 27–41:

```python
# tests/unit/cli/test_jira_smoke.py lines 27-41
def _fake_snapshot(issue_key: str = "ABC-123") -> JiraWorkSnapshot:
    return JiraWorkSnapshot(
        issue_key=issue_key,
        summary="Fix ETL job",
        description="desc",
        comments=({"id": "20001", "body": "first comment"},),
        changelog_ids=("10001",),
        links=(),
        repo_targets=(),
        context_hashes={"combined_hash": "hash"},
        classification={"kind": "bug", "urgency": "normal"},
        comment_commands=(),
        last_seen_comment_id="20001",
    )
```

---

#### Collector injection + stop event test pattern

Combine `test_jira_smoke.py` collector-lambda pattern (lines 58–65) with
`test_worker_shutdown.py` injectable-stop pattern (lines 383, 416–417):

```python
# Derived from test_jira_smoke.py lines 58-65 + test_worker_shutdown.py lines 383, 416
def test_watch_loop_calls_collector_per_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
    call_count = 0
    stop = threading.Event()

    def fake_collector(ref, *, timeout=15):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            stop.set()
        return _fake_snapshot()

    from whilly.cli.jira_watch_loop import _run_jira_watch
    rc = _run_jira_watch(
        # args namespace built from argparse or SimpleNamespace
        ...,
        snapshot_collector=fake_collector,
        stop_event=stop,               # pre-set event (test_worker_shutdown.py pattern)
        install_signal_handlers=False, # test_worker_shutdown.py line 416
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )
    assert call_count == 2
    assert rc == 0
```

---

#### WHILLY_LOG_DIR monkeypatch + file assertion pattern

Copy from `tests/unit/cli/test_jira_smoke.py` lines 55–73:

```python
# test_jira_smoke.py lines 55-73
monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
# ... run command ...
watch_dir = tmp_path / "watch"
assert watch_dir.is_dir()
status_file = watch_dir / "jira-watch-status.json"
assert status_file.exists()
status = json.loads(status_file.read_text(encoding="utf-8"))
assert status["state"] == "stopped"
```

---

#### _FakeRepo inject pattern for DB audit tests

Copy from `tests/unit/test_jira_watch.py` lines 108–119:

```python
# tests/unit/test_jira_watch.py lines 108-119
class _FakeRepo:
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    async def upsert_jira_work_session(self, **kwargs: object) -> dict[str, str]:
        self.upserts.append(kwargs)
        return {"issue_key": str(kwargs["issue_key"])}

    async def append_jira_work_event(self, **kwargs: object) -> int:
        self.events.append(kwargs)
        return 1
```

---

## Shared Patterns

### Exit codes
**Source:** `whilly/cli/jira.py` lines 45–47 and `whilly/cli/smoke.py` (EXIT_CONFIG_MISSING)
**Apply to:** `jira_watch_loop.py`

```python
# whilly/cli/jira.py lines 45-47
EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
# EXIT_CONFIG_MISSING imported from whilly.cli.smoke
```

### `_log_dir()` resolution
**Source:** `whilly/llm_ops.py` lines 30–31, 94–95
**Apply to:** `jira_watch_loop.py` — all path construction for status/pid files

```python
# whilly/llm_ops.py lines 94-95
def _log_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get(LOG_DIR_ENV) or DEFAULT_LOG_DIR).expanduser()
```

Watch-specific paths:
- Status file: `_log_dir() / "watch" / "jira-watch-status.json"`
- PID file: `_log_dir() / "watch" / "jira-watch.pid"`

### Callable type aliases
**Source:** `whilly/cli/jira.py` lines 49–58
**Apply to:** `jira_watch_loop.py` — redefine locally (do NOT import from `whilly.cli.jira` — circular)

```python
# whilly/cli/jira.py lines 49-58 — redefine these in jira_watch_loop.py
SnapshotCollector = Callable[..., JiraWorkSnapshot]
Runner = Callable[[Sequence[str]], int]
ConfigLoader = Callable[[], Any]
ConfigReader = Callable[[], dict[str, Any]]
Prompt = Callable[[str], str]
BrowserOpener = Callable[[str], bool]
IsATTY = Callable[[], bool]
```

### Best-effort DB persist (warn-not-fail)
**Source:** `whilly/cli/jira.py` lines 863–882
**Apply to:** Every `asyncio.run(_persist_watch_event(...))` call site in `jira_watch_loop.py`
**Rule:** Wrap in `try/except Exception` — DB down must never kill the watcher.

### `from __future__ import annotations`
**Source:** `whilly/cli/jira.py` line 16, `whilly/state_store.py` line 8
**Apply to:** All new Python files

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| PID-file single-instance guard (within `jira_watch_loop.py`) | utility | file-I/O | No existing pidfile in codebase; pattern derived from `StateStore.save()` atomic-write + stdlib `os.kill(pid, 0)` liveness check |

---

## Metadata

**Analog search scope:** `whilly/cli/`, `whilly/`, `tests/unit/cli/`, `tests/unit/`
**Files scanned:** `whilly/cli/jira.py` (1428 lines), `whilly/state_store.py` (133 lines),
`whilly/pause_control.py` (64 lines), `whilly/worker/main.py` (365 lines),
`tests/unit/cli/test_jira_smoke.py` (565 lines), `tests/unit/test_jira_watch.py` (119 lines),
`tests/unit/test_worker_shutdown.py` (426 lines)
**Pattern extraction date:** 2026-06-12
