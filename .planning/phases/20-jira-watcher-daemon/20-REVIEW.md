---
phase: 20-jira-watcher-daemon
reviewed: 2026-06-12T00:00:00Z
depth: standard
files_reviewed: 6
files_reviewed_list:
  - whilly/cli/jira_watch_loop.py
  - whilly/cli/jira.py
  - tests/unit/cli/test_jira_watch_loop.py
  - tests/unit/test_jira_cli.py
  - docs/Whilly-Usage.md
  - docs/Whilly-Interfaces-and-Tasks.md
findings:
  critical: 3
  warning: 7
  info: 6
  total: 16
status: issues_found
---

# Phase 20: Code Review Report

**Reviewed:** 2026-06-12
**Depth:** standard
**Files Reviewed:** 6
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

The core loop mechanics are solid where tests reach them: the signal handler only sets a `threading.Event` (reentrancy-safe), `_interruptible_sleep` uses `Event.wait` (no busy-spin), the status file write is genuinely atomic (mkstemp + `os.replace`), the PID guard never sends a terminating signal, and dispatch is structurally inside `if wants_dispatch`. The pause gate and default-off dispatch are well covered by unit tests.

The problems concentrate exactly where tests do NOT reach: the **production** `dispatch_runner` closure in `jira.py` (every test injects a fake runner). The production dispatch path crashes with `AttributeError` on first invocation and that exception kills the whole watcher; the readiness gate fails open when given the input the docs and help text tell operators to pass; and the `watch.dispatch` audit event is fabricated regardless of dispatch outcome — the exact Phase 18/19 bug class this review was asked to hunt. Multi-issue accounting is also dishonest (a later success erases an earlier failure's backoff in the same cycle).

## Critical Issues

### CR-01: Production dispatch path crashes with AttributeError and kills the entire watcher

**File:** `whilly/cli/jira.py:573` (calling `_run_argv` at `whilly/cli/jira.py:1522-1534`), uncaught at `whilly/cli/jira_watch_loop.py:668`
**Issue:** The dispatch closure ends with `_run_plan_worker(_run_argv(plan_id, dispatch_args))`. `_run_argv` reads `args.max_iterations`, `args.worker_id`, `args.verify_commands`, `args.optional_verify_commands`, and `args.verify_timeout` — **none of these attributes exist on the `watch` subparser namespace** (it only defines `issues`, `interval`, `timeout`, `dispatch`, `readiness_repo_path`, `allow_unready_run`, `interactive_config`, `no_interactive_config`; see `build_jira_parser` lines 361-409). The first time a real dispatch passes the config gate, the readiness gate, and the strict apply, it raises `AttributeError: 'Namespace' object has no attribute 'max_iterations'`. Worse, `_run_dispatch_if_ready` calls `dispatch_runner(args)` with **no try/except** (jira_watch_loop.py:668), so the exception propagates through the `while` loop and crashes the daemon with a traceback — the one component whose entire design promise is "backoff and keep going." No test exercises the production closure end-to-end; all dispatch tests inject fake runners, which is why this shipped green.
**Fix:**
```python
# jira.py — either add the pass-through flags to the watch subparser, or build a watch-safe argv:
def _watch_run_argv(plan_id: str) -> list[str]:
    return ["--plan", plan_id]
...
return _run_plan_worker(_watch_run_argv(plan_id))

# jira_watch_loop.py:_run_dispatch_if_ready — contain dispatch failures:
try:
    dispatch_rc = dispatch_runner(args)
except Exception:
    log.warning("dispatch failed; watcher continues", exc_info=False)
    dispatch_rc = EXIT_VALIDATION_ERROR
```
Add a test that runs `run_jira_command(["watch", "--issue", ..., "--dispatch"])` with only `snapshot_collector` faked, so the real closure executes.

### CR-02: Readiness gate fails open — documented `--readiness-repo-path` usage silently disables it

**File:** `whilly/cli/jira_watch_loop.py:625-632`, `whilly/cli/jira.py:391-394,560-561`, `docs/Whilly-Usage.md:166-171`
**Issue:** The help text ("Local repository path to inspect for code/test readiness") and the docs example (`--readiness-repo-path /path/to/repo`) tell operators to pass a **repository directory**. But `_run_dispatch_if_ready` passes that value straight to `_read_watch_readiness(plan_path)`, which does `plan_path.read_text()` — on a directory this raises `OSError` (IsADirectoryError) and returns `None`. The gate at line 632 only blocks when `readiness is not None`, so **the documented invocation bypasses the readiness gate entirely and dispatch proceeds for unready work**. The same fail-open shape applies to a missing or corrupt plan JSON. The closure's second gate (jira.py:560-561) is equally fail-open (`if readiness and ...`) against `out/jira-<RAW_REF>.json`, a file the watcher never creates (watch only collects snapshots; it never runs import), so in the common deployment **neither gate ever evaluates a verdict**. Tests only cover the case where the path happens to be a plan JSON file (`_make_plan_json`), which is not what the flag documents.
**Fix:** Decide one semantics and fail closed. Either (a) accept a repo directory and call `probe_code_readiness(Path(readiness_repo_path))` like `intake` does, or (b) accept a plan JSON path and rename the flag. In both cases, when readiness cannot be determined and `--dispatch` is set without `--allow-unready-run`, **block** (emit `watch.block` with `verdict="unknown"`) instead of dispatching:
```python
if not allow_unready and (readiness is None or readiness.get("verdict") != "ready_for_testing"):
    ...block...
```

### CR-03: `watch.dispatch` audit event fabricated regardless of dispatch outcome

**File:** `whilly/cli/jira_watch_loop.py:666-686`, `whilly/cli/jira.py:539-573`
**Issue:** `_run_dispatch_if_ready` calls `dispatch_runner(args)` and **discards the return code**, then unconditionally emits `EVENT_DISPATCH`. The production closure returns `EXIT_VALIDATION_ERROR` on config failure, missing issue, its internal readiness refusal, or a failed `apply --strict` — in every one of those cases the audit trail records `watch.dispatch` for an issue **every cycle, forever**, even though nothing was dispatched. This is exactly the fabricated-values bug class flagged in Phases 18/19. The dispatch result also never reaches the status file, so `watch-status` cannot distinguish "dispatching successfully" from "dispatch failing every cycle."
**Fix:**
```python
dispatch_rc = dispatch_runner(args)
status["last_dispatch_rc"] = dispatch_rc
_write_status(status, status_file)
payload = {"issue_key": issue_ref, "rc": dispatch_rc, "ok": dispatch_rc == 0}
# emit EVENT_DISPATCH with the honest payload (or a watch.dispatch_failed type on rc != 0)
```

## Warnings

### WR-01: Multi-issue cycles produce dishonest backoff, status, and failure events

**File:** `whilly/cli/jira_watch_loop.py:447-465,505-513`
**Issue:** Per-issue results overwrite shared state in order. With `--issue A --issue B` where A fails persistently and B succeeds: B's success resets `consecutive_failures = 0` and `backoff_seconds = 0` every cycle, so **backoff is never applied** for A; `last_poll_result` ends as `"ok"`; and the cycle event is emitted as `EVENT_FAILURE` (because `cycle_ok=False`) with a payload claiming `result: "ok", backoff_seconds: 0` — internally contradictory. `error_count` keeps climbing while the status file says everything is fine.
**Fix:** Track per-issue outcomes; apply backoff if *any* issue failed in the cycle, and make `last_poll_result` reflect the cycle (`"error"` if any failure, e.g. `"partial"`), with the event payload listing per-issue results.

### WR-02: PID lock has a TOCTOU race, and EPERM is treated as stale (fail-open)

**File:** `whilly/cli/jira_watch_loop.py:197-239`
**Issue:** (a) The check (`exists` → read → `os.kill(pid, 0)`) and the write (`mkstemp` + `os.replace`) are not atomic: two watchers started concurrently both pass the liveness check and both "acquire" — the single-instance guarantee breaks exactly under the race it exists for. (b) The comment calls treating `EPERM` as stale a "conservative choice", but it is the opposite: `EPERM` from `kill(pid, 0)` means the process **is alive** (owned by another user). The second watcher overwrites the lock and runs alongside the live one.
**Fix:** Use `os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` for creation (retry once after stale cleanup), and distinguish `ProcessLookupError` (stale) from `PermissionError` (alive → refuse):
```python
except ProcessLookupError:
    pass          # stale → reclaim
except PermissionError:
    return False  # alive, not ours → refuse
```

### WR-03: Credential gate never runs for `watch`; `--interactive-config` flags are dead

**File:** `whilly/cli/jira.py:505-581`, `whilly/cli/jira_watch_loop.py:341-342,386-387`
**Issue:** The watch subparser accepts `--interactive-config` / `--no-interactive-config`, but the watch branch never calls `_ensure_jira_config` (it is only invoked inside the dispatch closure, which is unreachable without `--dispatch`). Stale TODO comments in `jira_watch_loop.py` still say the gate "lands in plan 03 once the watch subparser is registered" — the subparser is now registered (this commit *is* plan 03) and the gate was not wired. With missing Jira config the daemon starts, every collect fails, and it loops forever at 60 s backoff with no actionable diagnostic, instead of exiting with the standard missing-config guidance that every other jira subcommand prints.
**Fix:** In the `watch` branch, run `_load_config()` + `_ensure_jira_config(args, ..., command_label="whilly jira watch")` before calling `_run_jira_watch`, returning its rc on failure. Remove the stale TODOs.

### WR-04: First poll is delayed by a full interval; comment describes logic that does not exist

**File:** `whilly/cli/jira_watch_loop.py:436-440`
**Issue:** The loop sleeps `interval + backoff` **before** the first collect. With the default 300 s interval, the watcher sits idle for 5 minutes after start with `state="running", last_poll_at=null`. The comment "skip on first cycle when interval==0" claims first-cycle skip behavior that is not implemented — it only "works" in tests because they use `interval=0`.
**Fix:** Poll first, then sleep (`collect → write status → sleep`), or guard the first iteration: `if cycle > 0 or backoff: _interruptible_sleep(...)`. Fix or delete the comment.

### WR-05: `watch-status` reports `state=running` forever after a crash

**File:** `whilly/cli/jira_watch_loop.py:555-605`
**Issue:** The final `"stopped"` status write only happens on a graceful exit path. After SIGKILL, OOM, or power loss, the status file permanently says `running` and `watch-status` repeats that claim without verifying the recorded PID — an operator (or automation) trusting it will believe a dead watcher is healthy. The acquire path already demonstrates the liveness check needed.
**Fix:** In `_run_watch_status`, when `state == "running"`, probe `os.kill(int(pid), 0)`; on `ProcessLookupError` report `state=stale (pid {pid} not running)`.

### WR-06: Dispatch re-fires for the same issue every cycle with no dedup, blocking the poll loop

**File:** `whilly/cli/jira_watch_loop.py:533-543,666-668`
**Issue:** There is no record of already-dispatched issues. Once an issue is `ready_for_testing`, every cycle re-runs `apply --strict` plus a **synchronous full worker run** (`_run_plan_worker` blocks until the worker finishes), then does it again next interval, indefinitely. Polling for all issues is frozen for the duration of each worker run, and the same plan is re-executed every ~interval.
**Fix:** Track dispatched issue keys (in `status`, e.g. `dispatched: {issue: timestamp}`) and skip re-dispatch unless the snapshot's `combined_hash` changed; at minimum document and rate-limit re-dispatch.

### WR-07: Dispatch closure uses the raw `--issue` value instead of the parsed Jira key; only `issues[0]` is ever dispatched

**File:** `whilly/cli/jira.py:548-558`
**Issue:** Help text says `--issue` accepts "Jira key or browse URL" (and `collect_jira_work_snapshot` indeed calls `parse_jira_key`). But the closure builds `plan_id = f"jira-{issue_ref.lower()}"` and `plan_path = Path("out") / f"jira-{issue_ref}.json"` from the **raw ref**. With a browse URL, both are garbage (`out/jira-https:/.../browse/ABC-123.json`), so apply fails every cycle (and per CR-03, `watch.dispatch` is still recorded). Also, only `issues[0]` is ever considered for dispatch; additional `--issue` values are polled but silently never dispatched. The relative `out/` path is additionally cwd-dependent.
**Fix:** `key = parse_jira_key(issue_ref)` (handling `ValueError`), use `key` for plan id/path consistent with `_run_import`; iterate dispatchable issues or document the first-issue-only limitation in `--dispatch` help.

## Info

### IN-01: Dead noqa'd import of `collect_jira_work_snapshot`

**File:** `whilly/cli/jira_watch_loop.py:35-38`
**Issue:** Imported with `# noqa: F401` "for production callers", but the only production caller (`jira.py:577`) imports it from `whilly.jira_watch` directly. Dead re-export.
**Fix:** Remove the import and the noqa.

### IN-02: Misleading comment about status on PID refusal

**File:** `whilly/cli/jira_watch_loop.py:389-391`
**Issue:** Comment says the interval is resolved early "so the status file can record it even when we refuse" — but the refusal path (lines 403-412) writes no status file at all (correctly: it must not clobber the live watcher's status).
**Fix:** Correct the comment.

### IN-03: Poll failures logged with zero error detail

**File:** `whilly/cli/jira_watch_loop.py:453-465`
**Issue:** The bare `except Exception` discards the exception entirely — the warning has no class or message, so operators cannot distinguish auth failure (will never recover; see WR-03) from a transient network blip. Secret-safety can be kept by logging only `exc.__class__.__name__`.
**Fix:** Include `exc.__class__.__name__` in the `log.warning` call and consider tracking it in `status["last_error"]`.

### IN-04: `watch-status` crashes on non-dict JSON; docs omit `watch.dispatch` from the event list

**File:** `whilly/cli/jira_watch_loop.py:586-594`; `docs/Whilly-Usage.md:175-176`
**Issue:** If the status file contains a JSON array/scalar, `data.get(...)` raises `AttributeError` (raw traceback). Separately, the Usage doc lists `watch.cycle`/`watch.failure`/`watch.paused`/`watch.block` but not the implemented `watch.dispatch` event.
**Fix:** Add `if not isinstance(data, dict): print(...could not read...); return EXIT_OK`. Add `watch.dispatch` to the docs list.

### IN-05: Test hygiene in `test_watch_dispatch_invokes_run_jira_watch`

**File:** `tests/unit/test_jira_cli.py:689-744`
**Issue:** Parameter annotated `tmp_path: pytest.MonkeyPatch` (it is a `Path`); the `stop = threading.Event(); stop.set()` setup and `_stopping_collector` are dead code (the spy replaces `_run_jira_watch`, so neither is used); the long comment block (709-716) describes a mechanism the test does not exercise. Misleading for future maintainers, and reinforces that no test runs the real watch loop through `run_jira_command`.
**Fix:** Strip dead code, fix the annotation, use `monkeypatch.setattr` instead of manual save/restore.

### IN-06: Inconsistent issue attribution and lost failure events in the paused branch

**File:** `whilly/cli/jira_watch_loop.py:483-500,518`
**Issue:** The paused event uses `issue_ref` (the loop variable left over from the for loop, i.e. the **last** issue), while cycle events use `issues[0]` — events for the same watcher are attributed to different issues. Also, when a cycle fails while paused, `continue` skips the `watch.failure` event entirely and `last_poll_result` overwrites `"error"` with `"paused"`, hiding the failure from the audit trail (error_count still increments, so counters and events disagree).
**Fix:** Pick one attribution convention (`issues[0]` or per-issue events) and include `cycle_ok`/`error_count` in the paused payload.

---

_Reviewed: 2026-06-12_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
