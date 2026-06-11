# Phase 19: Live Authenticated Smoke - Pattern Map

**Mapped:** 2026-06-12
**Files analyzed:** 7 (new/modified files)
**Analogs found:** 7 / 7

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `whilly/cli/jira.py` (add `smoke` action) | controller | request-response | `whilly/cli/jira.py` `_run_poll` (lines 636-669) | exact |
| `whilly/cli/gitlab.py` (new) | controller | request-response | `whilly/cli/qa_release.py` + `whilly/cli/rollback.py` | role-match |
| `whilly/cli/smoke.py` (new) | utility | transform | `tests/integration/test_alembic_full_chain.py` `_write_evidence` (lines 64-83) | data-flow-match |
| `whilly/cli/__init__.py` (register `gitlab`) | config | request-response | `whilly/cli/__init__.py` lines 445-498 | exact |
| `tests/unit/cli/test_jira_smoke.py` (new) | test | request-response | `tests/unit/test_jira_cli.py` lines 185-234 | exact |
| `tests/unit/cli/test_gitlab_smoke.py` (new) | test | request-response | `tests/unit/test_gitlab_mr.py` lines 1-47 | role-match |
| `docs/Whilly-Usage.md` (new section) | config | — | existing doc sections; constraint from `tests/unit/test_ui_parity_docs.py` | constraint |

---

## Pattern Assignments

### `whilly/cli/jira.py` — add `smoke` action

**Analog:** `whilly/cli/jira.py` — `_run_poll` and `build_jira_parser()` poll block

**Imports pattern** (lines 16-35 — already present, no new imports needed):
```python
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path

from whilly.jira_work import classify_jira_work
from whilly.jira_watch import JiraWorkSnapshot, collect_jira_work_snapshot, persist_jira_work_snapshot
from whilly.sources.jira import parse_jira_key

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
```

**Parser registration pattern** (lines 279-287 — `poll` parser as template):
```python
# whilly/cli/jira.py:279-287
p_poll = sub.add_parser(
    "poll",
    help="Run one Jira refresh cycle: issue, comments, changelog, remote links, and repo hints.",
)
p_poll.add_argument("jira_ref", help="Jira key or browse URL.")
p_poll.add_argument("--timeout", type=int, default=15, help="Per Jira HTTP request timeout in seconds.")
p_poll.add_argument("--plan-id", default="", help="Optional Whilly plan id to store with --persist.")
p_poll.add_argument("--persist", action="store_true", help="Persist the refreshed snapshot to Postgres.")
p_poll.add_argument("--json", action="store_true", help="Print the full snapshot as JSON.")
```
Copy verbatim for `p_smoke`; rename positional arg to `--issue` (required), drop `--plan-id`.

**Dispatch pattern** (lines 388-389 — `run_jira_command` dispatch block):
```python
# whilly/cli/jira.py:388-389
if args.action == "poll":
    return _run_poll(args, snapshot_collector=snapshot_collector or collect_jira_work_snapshot)
```
Add analogous branch for `smoke`:
```python
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

**Core action pattern** (lines 636-669 — `_run_poll`):
```python
# whilly/cli/jira.py:636-669
def _run_poll(args: argparse.Namespace, *, snapshot_collector: SnapshotCollector) -> int:
    try:
        snapshot = snapshot_collector(args.jira_ref, timeout=args.timeout)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"whilly jira poll: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    if args.persist:
        dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
        if not dsn:
            print("whilly jira poll: WHILLY_DATABASE_URL is required for --persist.", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        try:
            asyncio.run(_persist_poll_snapshot(dsn=dsn, snapshot=snapshot, plan_id=str(args.plan_id or "")))
        except Exception as exc:  # noqa: BLE001
            print(f"whilly jira poll: persist failed: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    else:
        classification = snapshot.classification
        print(
            "whilly jira poll: "
            f"issue={snapshot.issue_key} "
            f"classification={classification.get('kind', '')} "
            ...
        )
    return EXIT_OK
```
`_run_jira_smoke` follows the same shape: credential gate first (`_ensure_jira_config` before `snapshot_collector`), then per-check accumulation into a `SmokeReport`, then `write_smoke_report`, then optional `--persist`, then exit code derived from `SmokeReport.all_passed`.

**Credential gate pattern** (lines 994-1048 — `_ensure_jira_config`):
```python
# whilly/cli/jira.py:994-1048
def _ensure_jira_config(
    args: argparse.Namespace,
    *,
    config_reader: ConfigReader,
    env: MutableMapping[str, str],
    prompt: Prompt,
    secret_prompt: Prompt,
    browser_opener: BrowserOpener,
    stdin_isatty: IsATTY,
    command_label: str,
) -> int:
    state = _jira_config_state(config_reader(), env)
    missing = _missing_jira_settings(state)
    if not missing:
        return EXIT_OK
    interactive = bool(args.interactive_config) or (not bool(args.no_interactive_config) and stdin_isatty())
    if not interactive:
        _print_missing_jira_config(missing, command_label=command_label)
        return EXIT_VALIDATION_ERROR
    ...
```
Smoke calls `_ensure_jira_config` and returns immediately if it returns non-zero. This is `EXIT_CONFIG_MISSING = 2` (add in `smoke.py`).

**`_jira_config_state` / `_missing_jira_settings`** (lines 1051-1082):
```python
# whilly/cli/jira.py:1051-1082
def _jira_config_state(section: Mapping[str, Any], env: Mapping[str, str]) -> JiraConfigState:
    server_url = (
        env.get("JIRA_SERVER_URL") or env.get("WHILLY_JIRA_SERVER_URL") or _string_section_value(section, "server_url")
    ).strip()
    username = (
        env.get("JIRA_USERNAME") or env.get("WHILLY_JIRA_USERNAME") or _string_section_value(section, "username")
    ).strip()
    token_raw = env.get("JIRA_API_TOKEN") or _string_section_value(section, "token")
    token = _resolve_config_secret(token_raw).strip()
    return JiraConfigState(server_url=server_url, username=username, token=token, auth_scheme=auth_scheme)

def _missing_jira_settings(state: JiraConfigState) -> list[str]:
    missing: list[str] = []
    if not state.server_url:
        missing.append("JIRA_SERVER_URL")
    if state.auth_scheme == "basic" and not state.username:
        missing.append("JIRA_USERNAME")
    if not state.token:
        missing.append("JIRA_API_TOKEN")
    return missing
```

**Optional DB persist pattern** (lines 643-651):
```python
# whilly/cli/jira.py:643-651
if args.persist:
    dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
    if not dsn:
        print("whilly jira poll: WHILLY_DATABASE_URL is required for --persist.", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    try:
        asyncio.run(_persist_poll_snapshot(dsn=dsn, snapshot=snapshot, plan_id=str(args.plan_id or "")))
    except Exception as exc:  # noqa: BLE001
        print(f"whilly jira poll: persist failed: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
```
Smoke uses the same gate; `plan_id=""` is appropriate since smoke is not bound to a plan.

---

### `whilly/cli/gitlab.py` — new module

**Analog:** `whilly/cli/qa_release.py` (thin group, no credential state machine complexity) + `whilly/cli/rollback.py` (exit code constants + error surfacing pattern)

**Module structure pattern** — from `whilly/cli/qa_release.py` lines 1-73:
```python
# whilly/cli/qa_release.py:1-73
"""``whilly qa-release`` command surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2


def build_qa_release_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly qa-release",
        description="...",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="...")
    ...
    return parser


def run_qa_release_command(argv: Sequence[str]) -> int:
    parser = build_qa_release_parser()
    args = parser.parse_args(list(argv))
    if args.command == "collect":
        return _run_collect(args)
    ...
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USER_ERROR
```
For `gitlab.py`: rename `EXIT_USER_ERROR` to `EXIT_CHECK_FAILED = 1`, `EXIT_ENV_ERROR` to `EXIT_CONFIG_MISSING = 2` (or import from `smoke.py`). Parser is `build_gitlab_parser()`, entry is `run_gitlab_command(argv, *, gitlab_getter=None, environ=None)` for injection.

**Exit code constants** — from `whilly/cli/rollback.py` lines 20-24:
```python
# whilly/cli/rollback.py:20-24
EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
```
Mirror as `EXIT_OK = 0`, `EXIT_CHECK_FAILED = 1`, `EXIT_CONFIG_MISSING = 2`.

**Error surfacing** — from `whilly/cli/rollback.py` lines 62-84:
```python
# whilly/cli/rollback.py:62-84
def run_rollback_command(argv: Sequence[str]) -> int:
    parser = build_rollback_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    try:
        if args.command == "create":
            return _run_create(args)
        ...
    except RollbackError as exc:
        print(f"rollback {args.command}: {exc}", file=sys.stderr)
        return EXIT_USAGE
```
GitLab smoke catches per-check exceptions inside the accumulator loop, not at the top of `run_gitlab_command`; but the top-level `SystemExit` guard pattern applies unchanged.

**GitLab token resolution analog** — from `whilly/sinks/gitlab_mr.py` lines 78-102:
```python
# whilly/sinks/gitlab_mr.py:78-102
def _resolve_gitlab_token(host: str, env: dict[str, str] | None = None) -> str | None:
    src: dict[str, str] | os._Environ[str] = env if env is not None else os.environ
    token = src.get("GITLAB_API_TOKEN") or src.get("WHILLY_GITLAB_API_TOKEN")
    if token:
        return token.strip()
    try:
        result = subprocess.run(
            ["glab", "config", "get", "token", "-h", host],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None
```
`gitlab.py` adds a thin `_resolve_gitlab_config_state(env, host)` wrapper that also checks `GITLAB_TOKEN` and `GITLAB_URL` (the env vars named in CONTEXT.md) before falling through to the existing function.

**Infer remote host** — from `whilly/sinks/gitlab_mr.py` lines 54-75:
```python
# whilly/sinks/gitlab_mr.py:54-75
_REMOTE_HOST_RE = re.compile(r"(?:https?://|git@)([^/:]+)", re.IGNORECASE)

def _infer_remote_host(worktree_path: Path, git_bin: str = DEFAULT_GIT_BIN, timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            [git_bin, "config", "--get", "remote.origin.url"],
            cwd=str(worktree_path), capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            m = _REMOTE_HOST_RE.search(url)
            if m:
                return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return "gitlab.services.mts.ru"
```
Use `_REMOTE_HOST_RE` in `gitlab.py` to extract host from `--repo-url` for token resolution.

---

### `whilly/cli/smoke.py` — new shared helper

**Analog:** `tests/integration/test_alembic_full_chain.py` lines 44-83 (Phase 18 evidence pattern) + `whilly/llm_ops.py` lines 30-31, 94-95 (log dir convention)

**Evidence file pattern** (lines 64-83):
```python
# tests/integration/test_alembic_full_chain.py:64-83
@pytest.fixture(scope="session", autouse=True)
def _write_evidence() -> Iterator[None]:
    yield
    evidence = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "head_revision": EXPECTED_CHAIN[-1],
        "upgrade_ok": _RESULTS.get("upgrade_ok", False),
        "downgrade_ok": _RESULTS.get("downgrade_ok", False),
        "idempotent_ok": _RESULTS.get("idempotent_ok", False),
    }
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2))
```
Copy the timestamp format and boolean-per-check pattern. `smoke.py` lifts this into a `SmokeReport` dataclass with an `accumulate(name, passed, hint)` method, then `write_smoke_report(report_dir, kind, report_dict) -> Path`.

**Log dir convention** (lines 30-31, 94-95):
```python
# whilly/llm_ops.py:30-31
DEFAULT_LOG_DIR: Final[str] = "whilly_logs"
LOG_DIR_ENV: Final[str] = "WHILLY_LOG_DIR"

# whilly/llm_ops.py:94-95
def _log_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get(LOG_DIR_ENV) or DEFAULT_LOG_DIR).expanduser()
```
`smoke.py` imports or duplicates `DEFAULT_LOG_DIR` / `LOG_DIR_ENV` to resolve `whilly_logs/smoke/` from the same env var. Report directory is `_log_dir() / "smoke"`.

**Directory creation pattern** (line 145):
```python
# whilly/llm_ops.py:145
artifact_dir.mkdir(parents=True, exist_ok=True)
```
Use `parents=True, exist_ok=True` in `write_smoke_report()` before `path.write_text(...)`.

**`_write_json` helper** (lines 110-111):
```python
# whilly/llm_ops.py:110-111
def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
```
`write_smoke_report` follows the same `ensure_ascii=False, indent=2` + `encoding="utf-8"` convention; optionally add `sort_keys=True`.

**Exit code constants in `smoke.py`** (shared, imported by `jira.py` and `gitlab.py`):
```python
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG_MISSING = 2
```

---

### `whilly/cli/__init__.py` — register `whilly gitlab`

**Analog:** `whilly/cli/__init__.py` lines 445-498 (existing group registrations)

**`_HELP_TEXT` block** (lines 112-151):
```python
# whilly/cli/__init__.py:112-151  (abridged)
_HELP_TEXT = """\
Whilly v4 — distributed task orchestrator.

Usage: whilly <command> [options]

Commands:
  ...
  jira        Import Jira issues into Whilly plans.
  qa-release  Collect Jira release verification context and linked artifacts.
  rollback   Create/list rollback points ...
  ...
"""
```
Add `gitlab` entry after `jira` (alphabetically or logically grouped):
```
  gitlab      Smoke-test live GitLab integration (auth, repo-hint).
```
Also update the module docstring at lines 15-25 to include `whilly gitlab`.

**Lazy dispatch pattern** (lines 445-448):
```python
# whilly/cli/__init__.py:445-448
if cmd == "jira":
    from whilly.cli.jira import run_jira_command

    return run_jira_command(rest)
```
Add alongside it:
```python
if cmd == "gitlab":
    from whilly.cli.gitlab import run_gitlab_command

    return run_gitlab_command(rest)
```
No early imports — same lazy-import discipline as every other group.

---

### `tests/unit/cli/test_jira_smoke.py` — new test file

**Analog:** `tests/unit/test_jira_cli.py` (snapshot_collector injection + poll tests, lines 185-234)

**Test file header and env fixture** (lines 1-21):
```python
# tests/unit/test_jira_cli.py:1-21
"""Tests for the ``whilly jira`` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from whilly.cli.jira import JIRA_CLOUD_API_TOKEN_URL, run_jira_command
from whilly.jira_watch import JiraWorkSnapshot


def _jira_env() -> dict[str, str]:
    return {
        "JIRA_SERVER_URL": "https://company.atlassian.net",
        "JIRA_USERNAME": "dev@example.com",
        "JIRA_API_TOKEN": "jira-token",
    }
```

**Snapshot injection pattern** (lines 200-210):
```python
# tests/unit/test_jira_cli.py:200-210
rc = run_jira_command(
    ["poll", "ABC-123"],
    snapshot_collector=lambda ref, timeout=15: snapshot,
)
assert rc == 0
stdout = capsys.readouterr().out
assert "whilly jira poll: issue=ABC-123" in stdout
```
Smoke tests inject `snapshot_collector=lambda ref, timeout=15: snapshot` identically; they additionally check that the report file was written and contains no secrets.

**Missing config test pattern** (lines 226-234):
```python
# tests/unit/test_jira_cli.py:226-234
def test_jira_import_noninteractive_reports_missing_config_without_fetching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_jira_command(
        ["import", "ABC-123"],
        fetcher=lambda *_args, **_kwargs: pytest.fail("fetcher should not be called"),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ={},
    )
```
Smoke tests pass `environ={}` to trigger exit code 2; assert `rc == 2` and that the fetcher was never called.

---

### `tests/unit/cli/test_gitlab_smoke.py` — new test file

**Analog:** `tests/unit/test_gitlab_mr.py` lines 1-47 (import style + fixture helpers)

**Import + fixture helper pattern** (lines 1-47):
```python
# tests/unit/test_gitlab_mr.py:1-47
"""Unit tests for whilly.sinks.gitlab_mr — GitLab MR opener."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from whilly.sinks.gitlab_mr import _resolve_gitlab_token, open_mr_for_task


def _completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
```
Adapt for `gitlab.py`: import `run_gitlab_command` from `whilly.cli.gitlab`; inject `gitlab_getter` callable for HTTP mocking.

**Injection point for GitLab HTTP** (no existing analog — follows snapshot_collector pattern):
```python
# Pattern derived from test_jira_cli.py:200-203 (adapted)
rc = run_gitlab_command(
    ["smoke", "--repo-url", "https://gitlab.example.com/group/repo"],
    gitlab_getter=lambda url, *, token, timeout: {
        "id": 1,
        "path_with_namespace": "group/repo",
        "http_url_to_repo": "https://gitlab.example.com/group/repo.git",
    },
    environ={
        "GITLAB_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": "tok",
    },
)
assert rc == 0
```

---

### `docs/Whilly-Usage.md` — new "Live smoke" section

**Constraint (not analog):** `tests/unit/test_ui_parity_docs.py` lines 10-21:
```python
# tests/unit/test_ui_parity_docs.py:10-21
def test_operator_docs_pin_current_tui_wui_hotkeys() -> None:
    usage = _read("docs/Whilly-Usage.md")
    for text in (getting_started, usage):
        assert "1-5=switch" in text
        assert "p" in text
        assert "Pause workers" in text or "pause workers" in text
        assert "a/x/c" in text or "a / x / c" in text
        assert "q/d/l/t/h" not in text
```
The new "Live smoke" section MUST NOT introduce any of the prohibited strings. Adding a new `##` heading anywhere in the doc is safe; the test checks presence/absence of specific strings, not section ordering.

---

## Shared Patterns

### Credential gate — run before any external call
**Source:** `whilly/cli/jira.py:994-1048` (`_ensure_jira_config`)
**Apply to:** `_run_jira_smoke` (Jira smoke), `_run_gitlab_smoke` (analogous config check)

Pattern: call the config state function, check for missing keys, return `EXIT_CONFIG_MISSING = 2` before any HTTP call is made. This prevents `JiraAuth.from_config()` from raising a `RuntimeError` traceback.

### Per-check accumulation — never stop on first failure
**Source:** `tests/integration/test_alembic_full_chain.py:54-83` (`_RESULTS` dict + `_write_evidence`)
**Apply to:** `whilly/cli/smoke.py` (`SmokeReport`), `_run_jira_smoke`, `_run_gitlab_smoke`

Pattern:
```python
# Derived from test_alembic_full_chain.py:54-83
_RESULTS: dict[str, bool] = {}
# Each test sets _RESULTS["check_name"] = True only after it passes
# evidence = { key: _RESULTS.get(key, False) ... }
```
In `SmokeReport`: each check is wrapped in its own `try/except`; the exception is caught, converted to `{"passed": False, "hint": "..."}`, and the loop continues. Final exit code is `0` if all `passed=True`, else `1`.

### Secret redaction — never write tokens to report
**Source:** `tests/integration/test_alembic_full_chain.py:68-70`
```python
# test_alembic_full_chain.py:68-70
# No DSN / connection string is included (it carries the ephemeral container password).
```
**Apply to:** `whilly/cli/smoke.py` `write_smoke_report()`, `_run_jira_smoke`, `_run_gitlab_smoke`

Report payloads write only: `target_host` (hostname, not full URL with auth), `project_key`, `repo_path`, boolean check outcomes, counts. Never `JIRA_API_TOKEN`, `GITLAB_TOKEN`, or DSN.

### Optional DB persist gate
**Source:** `whilly/cli/jira.py:643-651` (`_run_poll` persist block)
**Apply to:** `_run_jira_smoke`, `_run_gitlab_smoke`

Pattern: `if args.persist:` → check `WHILLY_DATABASE_URL` → return `EXIT_CONFIG_MISSING` if missing → `asyncio.run(...)` with catch → return `EXIT_CHECK_FAILED` on failure. Missing `WHILLY_DATABASE_URL` without `--persist` is a silent no-op.

### Log dir derivation
**Source:** `whilly/llm_ops.py:30-31, 94-95`
**Apply to:** `whilly/cli/smoke.py`

```python
# whilly/llm_ops.py:30-31, 94-95
DEFAULT_LOG_DIR: Final[str] = "whilly_logs"
LOG_DIR_ENV: Final[str] = "WHILLY_LOG_DIR"

def _log_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get(LOG_DIR_ENV) or DEFAULT_LOG_DIR).expanduser()
```
`smoke.py` either imports `_log_dir` from `llm_ops` or duplicates the two constants. Report dir = `_log_dir() / "smoke"`.

### Lazy CLI group registration
**Source:** `whilly/cli/__init__.py:445-498`
**Apply to:** `whilly/cli/__init__.py` (`gitlab` registration)

Every group is imported inside the `if cmd == "..."` branch. No top-level imports in `__init__.py`. `_HELP_TEXT` and the module docstring must be updated in the same change as the dispatch branch.

---

## No Analog Found

No files in Phase 19 are entirely without an analog. The closest gaps are:

| File | Role | Data Flow | Note |
|---|---|---|---|
| `whilly/cli/smoke.py` — `SmokeReport` dataclass | utility | transform | Accumulator class has no direct analog; the evidence dict from Phase 18 provides the data model but not the class structure. Planner should model it as a `@dataclass` with `checks: list[dict]` and `add_check(name, passed, hint)` method. |
| `whilly/cli/gitlab.py` — `_gitlab_get` thin HTTP client | utility | request-response | No direct GitLab urllib client exists in the codebase. Mirror `_jira_get` from `whilly/sources/jira.py` (uses `urllib.request.urlopen`, Bearer header, timeout, JSON decode). |

---

## Metadata

**Analog search scope:** `whilly/cli/`, `whilly/sinks/`, `whilly/llm_ops.py`, `tests/unit/`, `tests/integration/test_alembic_full_chain.py`
**Files scanned:** 10 source files read
**Pattern extraction date:** 2026-06-12
