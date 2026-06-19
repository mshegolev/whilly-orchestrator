# Phase 19: Live Authenticated Smoke - Research

**Researched:** 2026-06-12
**Domain:** CLI smoke-test commands, Jira/GitLab credential wiring, report files
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- `whilly jira smoke` added as a new ACTION in the existing `whilly jira`
  subparser; new `whilly gitlab` CLI group with a `smoke` action.
- Jira checks reuse the poll-cycle code paths against an operator-supplied
  `--issue KEY`: auth/whoami, issue fetch, comments, changelog, remote links,
  classify.
- Strictly read-only — no comments posted, no transitions, safe against
  production Jira.
- Credentials use the existing resolution chain (JIRA_SERVER_URL / JIRA_USERNAME
  / JIRA_API_TOKEN env → config → interactive prompt, as in
  `whilly/cli/jira.py`); GitLab analogous (GITLAB_URL / GITLAB_TOKEN).
- JSON report plus human-readable stdout summary (mirrors the
  `migration-chain-evidence.json` pattern from Phase 18).
- Reports written to `whilly_logs/smoke/{jira|gitlab}-smoke-{timestamp}.json`.
- Content: per-check pass/fail, duration, redacted target info (server host,
  project key, repo path). Tokens/secrets are never written.
- DB audit event appended only when WHILLY_DATABASE_URL is set; the report file
  is primary and smoke must not hard-require a database.
- Exit codes: 0 = all checks pass, 1 = one or more checks failed,
  2 = configuration missing.
- Each failed check prints what the operator should verify — not a raw exception.
- New "Live smoke" section in `docs/Whilly-Usage.md` with setup steps.
- Tests: unit tests with mocked HTTP for check logic and failure hints; the live
  authenticated run is manual UAT.
- GitLab scope: token-authenticated API check plus link-refresh/repo-hint
  validation against a real repository URL.

### Claude's Discretion

- Internal module layout for the smoke checks (e.g., a shared smoke-report
  helper reused by both commands).
- Exact JSON schema field names; timestamp format; stdout formatting.
- How classify is invoked read-only against the fetched issue.

### Deferred Ideas (OUT OF SCOPE)

- Record/replay HTTP cassettes for CI execution of smoke logic.
- Write-path smoke checks (comment post / transition).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| LIVE-01 | Operator can run authenticated Jira smoke (classify, history, comments, links) against a real Jira project with documented setup. | `collect_jira_work_snapshot` in `whilly/jira_watch.py` fetches all four in one call; `classify_jira_work` from `whilly/jira_work.py` is a pure function reusable against the snapshot; `_ensure_jira_config` / `_jira_config_state` wiring in `whilly/cli/jira.py` handles credentials. |
| LIVE-02 | Operator can run authenticated GitLab smoke (link refresh, repo hints) against a real repository. | `_resolve_gitlab_token` in `whilly/sinks/gitlab_mr.py` gives the token-resolution chain; no direct GitLab HTTP client exists yet — the smoke must add one minimal GET to `/api/v4/projects/<id>` or use `glab api`; `collect_release_context` in `whilly/qa_release/collector.py` provides repo-hint extraction. |
| LIVE-03 | Smoke runs produce persisted audit evidence/reports an operator can review. | Phase 18's `_write_evidence` fixture pattern (`tests/integration/test_alembic_full_chain.py`) is the prior art; `whilly_logs/` dir creation uses `Path.mkdir(parents=True, exist_ok=True)` (see `whilly/llm_ops.py:145`). |
</phase_requirements>

---

## Summary

Phase 19 adds two read-only smoke commands — `whilly jira smoke` and
`whilly gitlab smoke` — that validate live integrations and leave a
timestamped JSON report file per run. The entire Jira check path already
exists in production code (`collect_jira_work_snapshot`, `JiraAuth.from_config`,
`_ensure_jira_config`, `classify_jira_work`); the smoke command wires them
together in read-only mode and accumulates pass/fail results. The GitLab smoke
is thinner: the existing `_resolve_gitlab_token` + `glab` token chain handles
credentials, but a minimal authenticated HTTP ping (GitLab `/api/v4/user` or
project metadata) must be added since no direct GitLab HTTP client exists today.

The report file pattern mirrors Phase 18: `Path.mkdir(parents=True, exist_ok=True)`,
`json.dumps` with ISO-8601 UTC timestamp, no secrets in output. DB persistence
follows the same optional `WHILLY_DATABASE_URL` gate used by `whilly jira poll
--persist`. The `whilly gitlab` CLI group registers in `whilly/cli/__init__.py`
exactly as other groups do (lazy import + `if cmd == "gitlab":` branch).

**Primary recommendation:** Implement a shared `whilly/cli/smoke.py` helper
(SmokeReport dataclass + `write_smoke_report()`) reused by both `jira.py` and
a new `whilly/cli/gitlab.py`; add the `smoke` action to `build_jira_parser()`
following the `poll` parser pattern; register `gitlab` in `main()` in
`whilly/cli/__init__.py`.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Credential resolution (Jira) | CLI (`whilly/cli/jira.py`) | Config layer (`whilly/config.py`) | `_jira_config_state` + `_ensure_jira_config` already own this |
| Credential resolution (GitLab) | CLI (`whilly/cli/gitlab.py`) | `whilly/sinks/gitlab_mr.py` | `_resolve_gitlab_token` + `glab config` is the existing resolution chain |
| Jira data fetching (smoke) | Domain (`whilly/jira_watch.py`) | Source adapter (`whilly/sources/jira.py`) | `collect_jira_work_snapshot` fetches issue/comments/changelog/links/repo-hints in one shot |
| Issue classification (smoke) | Domain (`whilly/jira_work.py`) | — | Pure function, no I/O, guaranteed read-only |
| GitLab API ping | New (`whilly/cli/gitlab.py`) | `whilly/sinks/gitlab_mr.py` | No direct GitLab HTTP client exists; new thin GET required |
| Report file persistence | New (`whilly/cli/smoke.py`) | `whilly_logs/smoke/` | Shared helper reused by both smoke commands |
| Optional DB audit event | `whilly/cli/jira.py` + `whilly/cli/gitlab.py` | `whilly/jira_watch.py:persist_jira_work_snapshot` | Same `WHILLY_DATABASE_URL` gate as `poll --persist` |
| Docs | `docs/Whilly-Usage.md` | — | New "Live smoke" section |

---

## Standard Stack

No new packages required. All capabilities are satisfied by the existing stdlib
and project modules.

### Core (existing, reused)

| Module | Location | Purpose in Smoke |
|--------|----------|-----------------|
| `JiraAuth.from_config()` | `whilly/sources/jira.py:74` | Resolve Jira credentials from env/config |
| `_jira_get` | `whilly/sources/jira.py:237` | Raw Jira HTTP GET (urllib, no requests) |
| `_jira_rest_path` | `whilly/sources/jira.py:293` | Build REST path with API version |
| `collect_jira_work_snapshot` | `whilly/jira_watch.py:58` | Fetch issue + comments + changelog + links + repo hints in one call |
| `classify_jira_work` | `whilly/jira_work.py:180` | Pure classification; no I/O (module docstring guarantees this) |
| `_jira_config_state` | `whilly/cli/jira.py:1051` | Build `JiraConfigState` from config + env |
| `_missing_jira_settings` | `whilly/cli/jira.py:1074` | List missing credential keys |
| `_ensure_jira_config` | `whilly/cli/jira.py:994` | Interactive/non-interactive credential gate |
| `_resolve_gitlab_token` | `whilly/sinks/gitlab_mr.py:78` | GitLab token from env / glab CLI |
| `_infer_remote_host` | `whilly/sinks/gitlab_mr.py:54` | Extract hostname from git remote URL |
| `collect_release_context` | `whilly/qa_release/collector.py:27` | Collect linked artifacts + repo hints from a Jira issue |
| `persist_jira_work_snapshot` | `whilly/jira_watch.py:128` | Async DB persistence (optional, same gate as poll) |

### New Modules to Create

| Module | Purpose |
|--------|---------|
| `whilly/cli/smoke.py` | `SmokeReport` dataclass + `write_smoke_report()` helper; timestamp format; redaction; path resolution |
| `whilly/cli/gitlab.py` | `build_gitlab_parser()` + `run_gitlab_command()` + `_run_smoke()` with thin GitLab API ping |

### Package Legitimacy Audit

> This phase installs **no new external packages**. All capabilities are
> provided by the Python stdlib (`urllib`, `json`, `dataclasses`, `datetime`,
> `pathlib`, `argparse`, `asyncio`) and existing project modules.

| Package | Status |
|---------|--------|
| No new packages | N/A |

---

## Architecture Patterns

### System Architecture Diagram

```
whilly jira smoke --issue KEY
        │
        ▼
_ensure_jira_config()            ← env / whilly.toml / interactive prompt
        │
        ▼ credentials OK
collect_jira_work_snapshot(KEY)  ← JiraAuth.from_config()
  ├─ _jira_get(issue + changelog)
  ├─ _jira_get(comments)
  └─ collect_release_context()
       └─ _jira_get(remote-links)
        │
        ▼ JiraWorkSnapshot
classify_jira_work(snapshot)     ← pure, no I/O
        │
        ▼ per-check results
SmokeReport.accumulate()
        │
        ├─ stdout summary (human-readable)
        ├─ write_smoke_report()  → whilly_logs/smoke/jira-smoke-{ts}.json
        └─ [optional] persist DB event if WHILLY_DATABASE_URL set


whilly gitlab smoke --repo-url URL
        │
        ▼
_resolve_gitlab_config()         ← GITLAB_URL / GITLAB_TOKEN env + glab CLI
        │
        ▼ credentials OK
_gitlab_get("/api/v4/user")      ← new thin HTTP GET (urllib, same pattern as _jira_get)
        │
        ▼ auth check
_resolve_project_from_url(URL)   ← parse repo path from URL
_gitlab_get("/api/v4/projects/{encoded_path}")
        │
        ▼ project metadata
collect_release_context(issue)   ← (re)uses Jira link parser for repo-hint extraction
  └─ _gitlab_hint() / _looks_like_gitlab_url()
        │
        ▼ per-check results
SmokeReport.accumulate()
        │
        ├─ stdout summary
        ├─ write_smoke_report()  → whilly_logs/smoke/gitlab-smoke-{ts}.json
        └─ [optional] persist DB event
```

### Recommended Project Structure

```
whilly/cli/
├── jira.py          # add `smoke` action to build_jira_parser() + _run_smoke()
├── gitlab.py        # NEW: build_gitlab_parser(), run_gitlab_command(), _run_smoke()
└── smoke.py         # NEW: SmokeReport, write_smoke_report(), _redact_url()

tests/unit/cli/
├── test_jira_smoke.py    # NEW: unit tests for jira smoke logic
└── test_gitlab_smoke.py  # NEW: unit tests for gitlab smoke logic

whilly_logs/smoke/
├── jira-smoke-2026-06-12T10:00:00Z.json
└── gitlab-smoke-2026-06-12T10:01:00Z.json
```

### Pattern 1: Adding `smoke` ACTION to existing jira subparser

The `poll` action in `build_jira_parser()` is the exact template to follow.
[VERIFIED: codebase, `whilly/cli/jira.py:279-290`]

```python
# Source: whilly/cli/jira.py:279 (poll template)
p_smoke = sub.add_parser(
    "smoke",
    help=(
        "Authenticated smoke: auth/whoami, issue fetch, comments, "
        "changelog, remote links, classify. Exits 0=pass, 1=fail, "
        "2=config missing."
    ),
)
p_smoke.add_argument(
    "--issue",
    required=True,
    help="Jira key or browse URL, e.g. ABC-123.",
)
p_smoke.add_argument(
    "--timeout",
    type=int,
    default=15,
    help="Per Jira HTTP request timeout in seconds (default: 15).",
)
p_smoke.add_argument(
    "--persist",
    action="store_true",
    help="Append a DB audit event (requires WHILLY_DATABASE_URL).",
)
p_smoke.add_argument(
    "--json",
    action="store_true",
    help="Print the full smoke report as JSON.",
)
```

In `run_jira_command()`, add to the dispatch block:
[VERIFIED: codebase, `whilly/cli/jira.py:354-404`]

```python
if args.action == "smoke":
    return _run_jira_smoke(
        args,
        snapshot_collector=snapshot_collector
            or collect_jira_work_snapshot,
        config_loader=config_loader,
        config_reader=config_reader,
        environ=environ,
        prompt=prompt,
        secret_prompt=secret_prompt,
        browser_opener=browser_opener,
        stdin_isatty=stdin_isatty,
    )
```

### Pattern 2: Registering `whilly gitlab` CLI group

In `whilly/cli/__init__.py`, add the lazy-import branch alongside other groups.
[VERIFIED: codebase, `whilly/cli/__init__.py:445-448`]

```python
if cmd == "gitlab":
    from whilly.cli.gitlab import run_gitlab_command
    return run_gitlab_command(rest)
```

Also update `_HELP_TEXT` in `whilly/cli/__init__.py` to include the new
`gitlab` entry (line ~143).

### Pattern 3: Report file — Phase 18 evidence pattern

[VERIFIED: codebase, `tests/integration/test_alembic_full_chain.py:63-83`]

```python
# Source: tests/integration/test_alembic_full_chain.py:76
import datetime, json
from pathlib import Path

def write_smoke_report(report_dir: Path, report: dict) -> Path:
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    kind = report.get("kind", "smoke")
    path = report_dir / f"{kind}-smoke-{ts}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
```

Key conventions from Phase 18 evidence file:
- `"timestamp"` key holds the UTC ISO-8601 string
- Booleans (`True`/`False`) for each check result
- No DSN, no token, no password in the payload
- Path anchored from `REPO_ROOT` / `whilly_logs/`

### Pattern 4: Optional DB audit event

Same gate as `poll --persist`. [VERIFIED: codebase, `whilly/cli/jira.py:643-651`]

```python
# Source: whilly/cli/jira.py:643 (_run_poll)
if args.persist:
    dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
    if not dsn:
        print(
            "whilly jira smoke: WHILLY_DATABASE_URL is required "
            "for --persist.",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_ERROR
    try:
        asyncio.run(
            _persist_poll_snapshot(
                dsn=dsn,
                snapshot=snapshot,
                plan_id="",
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"whilly jira smoke: persist failed: {exc}",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_ERROR
```

`persist_jira_work_snapshot` already inserts a `jira.refreshed` event type;
for smoke the `state` argument can be `"smoke_checked"` to distinguish from
regular poll cycles.

### Pattern 5: GitLab credential resolution chain

[VERIFIED: codebase, `whilly/sinks/gitlab_mr.py:78-102`]

Lookup order (mirrors the existing `_resolve_gitlab_token`):
1. `GITLAB_TOKEN` env var
2. `GITLAB_API_TOKEN` env var (or `WHILLY_GITLAB_API_TOKEN`)
3. `glab config get token -h <host>` CLI fallback

The CONTEXT.md uses `GITLAB_URL / GITLAB_TOKEN` as the env var names for
GitLab smoke. The existing `_resolve_gitlab_token` already covers 2 and 3;
a thin wrapper should also check `GITLAB_TOKEN` (no `_API_` infix) since
that is what the CONTEXT specifies.

```python
def _resolve_gitlab_config_state(
    env: Mapping[str, str],
    host: str,
) -> tuple[str, str]:
    """Return (url, token). Empty string means missing."""
    url = (
        env.get("GITLAB_URL")
        or env.get("WHILLY_GITLAB_URL")
        or ""
    ).rstrip("/")
    token = (
        env.get("GITLAB_TOKEN")
        or env.get("GITLAB_API_TOKEN")
        or env.get("WHILLY_GITLAB_API_TOKEN")
        or _glab_token(host)
        or ""
    )
    return url, token
```

### Pattern 6: Unit test injection pattern for smoke checks

Follow the `snapshot_collector` lambda injection pattern from
`test_jira_cli.py:200-203`. [VERIFIED: codebase]

```python
# Source: tests/unit/test_jira_cli.py:200
rc = run_jira_command(
    ["smoke", "--issue", "ABC-123"],
    snapshot_collector=lambda ref, timeout=15: snapshot,
    config_loader=lambda: None,
    config_reader=lambda: {},
    environ={
        "JIRA_SERVER_URL": "https://j.example.com",
        "JIRA_USERNAME": "u",
        "JIRA_API_TOKEN": "t",
    },
)
assert rc == 0
```

For GitLab, inject a `gitlab_getter` callable that replaces the HTTP GET:

```python
rc = run_gitlab_command(
    ["smoke", "--repo-url",
     "https://gitlab.example.com/group/repo"],
    gitlab_getter=lambda url, *, token, timeout: {
        "id": 1,
        "path_with_namespace": "group/repo",
    },
    environ={
        "GITLAB_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": "tok",
    },
)
assert rc == 0
```

### Anti-Patterns to Avoid

- **Raising exceptions to the operator.** All `RuntimeError` and `OSError`
  from `_jira_get` / the GitLab ping must be caught and converted to a
  per-check `{"pass": False, "hint": "..."}` entry, never a traceback.
- **Writing credentials to the report.** `JIRA_API_TOKEN`, `GITLAB_TOKEN`,
  and any password must not appear in `whilly_logs/smoke/*.json`. Redact to
  the URL host only.
- **Requiring DB for smoke.** `--persist` is opt-in. Missing
  `WHILLY_DATABASE_URL` without `--persist` must be a no-op.
- **Side-effecting Jira/GitLab.** Smoke is read-only; never call write
  endpoints. `classify_jira_work` is guaranteed read-only by its module
  docstring.
- **Hard-coding `whilly_logs/smoke/` path.** Derive from
  `WhillyConfig.LOG_DIR` (default `"whilly_logs"`) so operators with a
  custom `WHILLY_LOG_DIR` still get consistent placement.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Jira HTTP client | Custom HTTP session | `_jira_get` + `JiraAuth` in `whilly/sources/jira.py` | Already handles basic/PAT auth, SSL, proxy bypass, ADF decode |
| Jira issue fetch | Custom REST call | `collect_jira_work_snapshot` in `whilly/jira_watch.py` | Fetches issue, comments, changelog, remote links, repo hints in one composed call |
| Issue classification | Custom keyword logic | `classify_jira_work` in `whilly/jira_work.py` | Pure, tested, no I/O side effects guaranteed by module docstring |
| Jira credential state machine | Custom env reading | `_jira_config_state` + `_ensure_jira_config` in `whilly/cli/jira.py` | Handles toml section, env vars, WHILLY_ prefix variants, PAT/basic, interactive prompt fallback |
| GitLab token resolution | Custom env probe | `_resolve_gitlab_token` in `whilly/sinks/gitlab_mr.py` | Checks `GITLAB_API_TOKEN`, `WHILLY_GITLAB_API_TOKEN`, `glab config get token -h HOST` in order |
| Report file write | Custom timestamp/path | Phase 18 evidence pattern (`test_alembic_full_chain.py:76`) | UTC ISO-8601 Z format, `mkdir(parents=True, exist_ok=True)`, no secrets rule |
| DB persistence | New Postgres code | `persist_jira_work_snapshot` + `asyncio.run()` gate in `_run_poll` pattern | Protocol-based repo, async pool lifecycle already managed |

---

## Common Pitfalls

### Pitfall 1: `JiraAuth.from_config()` raises instead of returning `None`

**What goes wrong:** `JiraAuth.from_config()` raises `RuntimeError` when any
required credential is missing. Calling it before `_ensure_jira_config` runs
means the smoke command crashes with a raw traceback instead of the actionable
config hint.
**Why it happens:** `collect_jira_work_snapshot` calls `JiraAuth.from_config()`
internally. If called before credential validation, `RuntimeError` propagates
through `collect_jira_work_snapshot`.
**How to avoid:** Always run `_ensure_jira_config` first and return early with
exit code 2 before calling `collect_jira_work_snapshot`. This mirrors the
pattern in `_run_poll` (config gate before snapshot collection).
[VERIFIED: `whilly/sources/jira.py:129-138`, `whilly/cli/jira.py:636-640`]

### Pitfall 2: Per-check failure must not stop subsequent checks

**What goes wrong:** A `try/except` that returns on the first failed check
produces exit code 1 but skips the remaining checks (e.g., a comment fetch
failure prevents the classify check from running).
**Why it happens:** Natural exception propagation pattern.
**How to avoid:** Use an accumulator (`SmokeReport`) pattern: each check is
wrapped independently; exceptions set `{"pass": False, "hint": "..."}` and
the loop continues. Only after all checks are accumulated do we write the
report and return the final exit code.
**Warning signs:** Tests that only verify the first failing check but not
that subsequent checks still ran.

### Pitfall 3: Token leaked to report file

**What goes wrong:** Naively putting `snapshot.to_dict()` or the GitLab
project payload into the report file may indirectly include credential info
that appeared in URL fields.
**Why it happens:** Some Jira self-hosted deployments embed credentials in
the server URL.
**How to avoid:** Write a `_redact_url(url: str) -> str` helper that strips
authority components (`user:pass@host` → `host`) before writing. Never write
raw env values. The check outputs should be `host`, `project_key`, and
boolean outcomes only.

### Pitfall 4: `whilly_logs/smoke/` dir creation timing

**What goes wrong:** `write_smoke_report()` called before the directory exists
raises `FileNotFoundError`.
**Why it happens:** `whilly_logs/` itself may not exist on a clean machine.
**How to avoid:** `Path.mkdir(parents=True, exist_ok=True)` before
`Path.write_text(...)`. The `parents=True` flag creates both `whilly_logs/`
and `whilly_logs/smoke/` in one call.
[VERIFIED: `whilly/llm_ops.py:145`]

### Pitfall 5: `whilly gitlab` missing from `_HELP_TEXT`

**What goes wrong:** `whilly --help` lists all commands but omits `gitlab`,
confusing operators.
**Why it happens:** `_HELP_TEXT` in `whilly/cli/__init__.py` is a static
string that must be updated manually when a new command group is added.
**How to avoid:** The plan task for registering the CLI group must include
updating `_HELP_TEXT` in the same diff.
[VERIFIED: `whilly/cli/__init__.py:112-150`]

### Pitfall 6: GitLab API version coupling

**What goes wrong:** Hard-coding `/api/v4/` in the GitLab ping means a
self-hosted GitLab with a different API prefix silently fails.
**Why it happens:** GitLab SaaS and most self-hosted instances use `/api/v4/`,
but the URL base can vary.
**How to avoid:** Derive the API base from `GITLAB_URL`, defaulting to
`{GITLAB_URL}/api/v4`. Treat "HTTP 200 with valid JSON" as the success
criterion, not a specific response shape.

### Pitfall 7: Docs regression test breakage

**What goes wrong:** `tests/unit/test_ui_parity_docs.py` asserts specific
strings in `docs/Whilly-Usage.md` (e.g., `"1-5=switch"`, `"p"`,
`"Pause workers"`, `"a/x/c"`, absence of `"q/d/l/t/h"`).
Adding a "Live smoke" section must not disturb those anchors.
**Why it happens:** `test_operator_docs_pin_current_tui_wui_hotkeys` reads
the entire file and checks for presence/absence of strings.
**How to avoid:** The new "Live smoke" section should not introduce any of
the prohibited strings. Safe to add a new `##` section anywhere in the doc.
[VERIFIED: `tests/unit/test_ui_parity_docs.py:10-21`]

---

## Code Examples

### Calling `collect_jira_work_snapshot` (the poll code path)

```python
# Source: whilly/jira_watch.py:58-75 [VERIFIED]
from whilly.jira_watch import collect_jira_work_snapshot

snapshot = collect_jira_work_snapshot("ABC-123", timeout=15)
# snapshot.issue_key       → "ABC-123"
# snapshot.comments        → tuple of comment dicts
# snapshot.changelog_ids   → tuple of str
# snapshot.links           → tuple of link dicts
# snapshot.repo_targets    → tuple of repo hint dicts
# snapshot.classification  → dict {"kind": ..., "urgency": ...}
# snapshot.last_seen_comment_id → str
```

### Credential state check (Jira)

```python
# Source: whilly/cli/jira.py:1051-1082 [VERIFIED]
from whilly.cli.jira import (
    _jira_config_state,
    _missing_jira_settings,
    _ensure_jira_config,
    EXIT_OK,
    EXIT_VALIDATION_ERROR,
)

state = _jira_config_state(config_reader(), env)
missing = _missing_jira_settings(state)
if missing:
    return EXIT_VALIDATION_ERROR  # or run _ensure_jira_config
```

### Writing a report file (Phase 18 pattern)

```python
# Source: tests/integration/test_alembic_full_chain.py:75-83 [VERIFIED]
import datetime, json
from pathlib import Path

ts = (
    datetime.datetime.now(datetime.timezone.utc)
    .isoformat()
    .replace("+00:00", "Z")
)
report = {
    "timestamp": ts,
    "kind": "jira",
    "target_host": "jira.example.com",   # redacted — no token
    "project_key": "ABC",
    "checks": {
        "auth": True,
        "issue_fetch": True,
        "comments": True,
        "changelog": True,
        "remote_links": True,
        "classify": True,
    },
    "classification_kind": "bug",
    "classification_urgency": "normal",
    "comment_count": 3,
    "changelog_count": 7,
    "link_count": 2,
    "repo_target_count": 1,
    "duration_seconds": 1.23,
}
report_dir = Path("whilly_logs") / "smoke"
report_dir.mkdir(parents=True, exist_ok=True)
path = report_dir / f"jira-smoke-{ts}.json"
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
```

### Handling optional DB persist (same as poll)

```python
# Source: whilly/cli/jira.py:643-651 [VERIFIED]
import asyncio, os, sys

if args.persist:
    dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
    if not dsn:
        print(
            "whilly jira smoke: WHILLY_DATABASE_URL is "
            "required for --persist.",
            file=sys.stderr,
        )
        return 2   # EXIT_CONFIG_MISSING
    asyncio.run(
        _persist_poll_snapshot(
            dsn=dsn,
            snapshot=snapshot,
            plan_id="",
        )
    )
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual Jira ping via curl | `collect_jira_work_snapshot` in `jira_watch.py` | Phase 17 | Fetches all fields atomically; classify is pure; all paths tested |
| `whilly --from-jira KEY` (v3 legacy flag) | `whilly jira import KEY` (v4 subcommand) | v4 migration | Legacy shim in `whilly/cli/__init__.py:314` still translates it |
| Migration evidence as text log | JSON evidence file with boolean flags | Phase 18 | Machine-readable, grep-safe, no secrets |

**No deprecated patterns to avoid for this phase.**

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | CONTEXT.md specifies `GITLAB_URL` / `GITLAB_TOKEN` as the GitLab env var names; but existing code uses `GITLAB_API_TOKEN` / `WHILLY_GITLAB_API_TOKEN`. The smoke command should accept all three. | Standard Stack — GitLab credential resolution | If only `GITLAB_TOKEN` is checked, operators who already set `GITLAB_API_TOKEN` for the MR sink would need to re-set a differently named var. |
| A2 | The GitLab smoke uses a direct HTTP GET (`urllib` + Bearer token) to `/api/v4/user` or `/api/v4/projects/{path}` rather than the `glab` CLI, for speed and testability. | Standard Stack | If `glab api` is preferred for consistency, the implementation changes but the test injection pattern stays the same. |
| A3 | `whilly_logs/smoke/` is relative to the operator's current working directory (matching how `WhillyConfig.LOG_DIR` is resolved), not hardcoded to the repo root. | Common Pitfalls #4 | If relative, a `cd` before running smoke changes where reports land; operators may need to set `WHILLY_LOG_DIR` explicitly. |

**If this table is empty:** All claims in this research were verified or cited —
no user confirmation needed. (It is not empty; A1-A3 require planner awareness.)

---

## Open Questions (RESOLVED)

1. **GitLab "whoami" check — `glab api` vs. direct `urllib` HTTP GET**
   - What we know: `_resolve_gitlab_token` already resolves a Bearer token;
     `_jira_get` shows the stdlib urllib pattern is the project standard.
   - What's unclear: CONTEXT.md says "token-authenticated API check" without
     specifying the mechanism. Using `glab api /api/v4/user` is simpler to
     implement (no new HTTP code) but harder to unit-test without mocking
     subprocess; a direct `urllib` GET mirrors `_jira_get` and is injectable.
   - Recommendation (Claude's discretion): Implement a thin `_gitlab_get(url,
     *, token, timeout)` using `urllib.request` (same as `_jira_get` pattern)
     for testability. The test can inject a `gitlab_getter` callable.

2. **Which GitLab API endpoint for "link-refresh" check?**
   - What we know: CONTEXT says `link-refresh and repo-hint checks`. The
     `collect_release_context` function (used inside `collect_jira_work_snapshot`)
     extracts repo hints from Jira remote links — it never calls the GitLab API.
     The "link refresh" in the Jira poll cycle means refreshing the remote-link
     set from Jira, not from GitLab.
   - What's unclear: For `whilly gitlab smoke`, there is no Jira issue to poll.
     The "link-refresh" check may mean: given a GitLab project URL, verify the
     repo is accessible and repo-hint extraction works on that URL.
   - Recommendation: Implement `whilly gitlab smoke --repo-url URL` as:
     (a) token auth ping to `/api/v4/projects/{encoded_path}`, (b) parse
     repo-hint from the URL via `_gitlab_hint()` from `qa_release/collector.py`,
     (c) verify the project's `http_url_to_repo` matches the clone URL — all
     read-only.

3. **`EXIT_CONFIG_MISSING` constant naming**
   - What we know: CONTEXT specifies exit code 2 for config missing, exit 1
     for check failures, exit 0 for all pass. Current `jira.py` only defines
     `EXIT_OK = 0` and `EXIT_VALIDATION_ERROR = 1`.
   - Recommendation: Add `EXIT_CONFIG_MISSING = 2` to `whilly/cli/smoke.py`
     (shared) and import it in both `jira.py` and `gitlab.py`.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python stdlib (`urllib`, `json`, `pathlib`, `datetime`, `asyncio`) | All checks | ✓ | 3.10+ | — |
| `whilly/sources/jira.py` | Jira smoke | ✓ (project code) | current | — |
| `whilly/jira_watch.py` | Jira smoke | ✓ (project code) | current | — |
| `whilly/sinks/gitlab_mr.py` | GitLab token resolution | ✓ (project code) | current | — |
| `glab` CLI | GitLab token fallback | [ASSUMED] — present in Acme dev env per CLAUDE.md but not verified at research time | — | `GITLAB_TOKEN` / `GITLAB_API_TOKEN` env var (no glab needed) |
| `WHILLY_DATABASE_URL` | `--persist` flag only | Not required | — | Skip DB event; write report file only |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback:** `glab` CLI — if absent, token must be
set via `GITLAB_TOKEN` env var; the smoke command should emit a friendly hint
rather than failing.

---

## Validation Architecture

> `nyquist_validation` is `true` in `.planning/config.json` — section included.

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest >= 8.0 with asyncio_mode = "auto" |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/unit/cli/test_jira_smoke.py tests/unit/cli/test_gitlab_smoke.py -q` |
| Full suite command | `pytest -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| LIVE-01 | Jira smoke all-checks-pass path returns exit 0 | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_all_checks_pass -x` | ❌ Wave 0 |
| LIVE-01 | Jira smoke missing config returns exit 2 | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_missing_config -x` | ❌ Wave 0 |
| LIVE-01 | Jira smoke one failed check returns exit 1 | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_check_failure -x` | ❌ Wave 0 |
| LIVE-01 | Failed check prints actionable hint, not traceback | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_failure_hint -x` | ❌ Wave 0 |
| LIVE-01 | classify check runs read-only against snapshot | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_classify_readonly -x` | ❌ Wave 0 |
| LIVE-02 | GitLab smoke token-auth check returns exit 0 | unit | `pytest tests/unit/cli/test_gitlab_smoke.py::test_gitlab_smoke_auth_pass -x` | ❌ Wave 0 |
| LIVE-02 | GitLab smoke missing GITLAB_URL returns exit 2 | unit | `pytest tests/unit/cli/test_gitlab_smoke.py::test_gitlab_smoke_missing_config -x` | ❌ Wave 0 |
| LIVE-02 | Repo-hint extracted from --repo-url | unit | `pytest tests/unit/cli/test_gitlab_smoke.py::test_gitlab_smoke_repo_hint -x` | ❌ Wave 0 |
| LIVE-03 | Report file written to whilly_logs/smoke/ | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_writes_report -x` | ❌ Wave 0 |
| LIVE-03 | Report file contains no secrets | unit | `pytest tests/unit/cli/test_jira_smoke.py::test_jira_smoke_report_no_secrets -x` | ❌ Wave 0 |
| LIVE-03 | GitLab report file written | unit | `pytest tests/unit/cli/test_gitlab_smoke.py::test_gitlab_smoke_writes_report -x` | ❌ Wave 0 |
| LIVE-01/02 | Docs regression: Whilly-Usage.md smoke section present | unit | `pytest tests/unit/test_ui_parity_docs.py -q` | ✅ (existing; must keep passing) |
| LIVE-01 | Live Jira auth UAT | manual | `JIRA_SERVER_URL=... whilly jira smoke --issue KEY` | — (manual UAT only) |
| LIVE-02 | Live GitLab auth UAT | manual | `GITLAB_URL=... GITLAB_TOKEN=... whilly gitlab smoke --repo-url URL` | — (manual UAT only) |

### Sampling Rate

- **Per task commit:** `pytest tests/unit/cli/test_jira_smoke.py tests/unit/cli/test_gitlab_smoke.py -q`
- **Per wave merge:** `pytest -q`
- **Phase gate:** Full suite green before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/unit/cli/__init__.py` — make directory a package
- [ ] `tests/unit/cli/test_jira_smoke.py` — unit tests for `_run_jira_smoke`
- [ ] `tests/unit/cli/test_gitlab_smoke.py` — unit tests for GitLab smoke
- [ ] `whilly/cli/smoke.py` — shared SmokeReport helper (Wave 0 stub)
- [ ] `whilly/cli/gitlab.py` — new module skeleton (Wave 0 stub)
- [ ] `pyproject.toml` — add `live_smoke` marker (optional, for future UAT gating)

---

## Security Domain

> `security_enforcement` is not explicitly `false` — section included.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes (verifying credentials resolve correctly) | Existing `JiraAuth.from_config()` / `_resolve_gitlab_token` |
| V3 Session Management | no | Smoke is stateless CLI |
| V4 Access Control | no | Read-only; no privilege escalation surface |
| V5 Input Validation | yes (`--issue KEY` must parse as valid Jira key) | `parse_jira_key()` from `whilly/sources/jira.py` already validates |
| V6 Cryptography | no | Token transmission over existing HTTPS; no new crypto |

### Known Threat Patterns for This Stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Token written to report file | Information Disclosure | Redact all secrets before `json.dumps`; write only host, booleans, counts |
| Path traversal via `--issue` argument | Tampering | `parse_jira_key()` enforces `[A-Z][A-Z0-9]+-\d+` pattern before any file writes |
| Malicious GitLab server response used as path component | Tampering | `_normalize_repo_path()` from `whilly/cli/jira.py:834` strips `../` sequences; use the same helper for report filename components |
| Credential in `whilly_logs/smoke/` dir readable by other local users | Information Disclosure | Report file must never contain token; `mkdir(mode=0o700)` is an option for sensitive deployments (Claude's discretion) |

---

## Sources

### Primary (HIGH confidence)

- Codebase, `whilly/cli/jira.py` — `build_jira_parser()`, `run_jira_command()`,
  `_run_poll()`, `_ensure_jira_config()`, `_jira_config_state()`,
  `_missing_jira_settings()` (all verified line-by-line)
- Codebase, `whilly/jira_watch.py` — `collect_jira_work_snapshot()`,
  `JiraWorkSnapshot`, `persist_jira_work_snapshot()` (verified)
- Codebase, `whilly/sources/jira.py` — `JiraAuth.from_config()`, `_jira_get()`,
  `_jira_rest_path()` (verified)
- Codebase, `whilly/jira_work.py` — `classify_jira_work()`, module read-only
  guarantee in docstring line 3 (verified)
- Codebase, `whilly/cli/__init__.py` — `main()` dispatch pattern, lazy-import
  block, `_HELP_TEXT` (verified)
- Codebase, `whilly/sinks/gitlab_mr.py` — `_resolve_gitlab_token()`,
  `_infer_remote_host()` (verified)
- Codebase, `whilly/qa_release/collector.py` — `collect_release_context()`,
  `_gitlab_hint()`, `_looks_like_gitlab_url()` (verified)
- Codebase, `tests/integration/test_alembic_full_chain.py:46-83` — Phase 18
  evidence file pattern (verified)
- Codebase, `tests/unit/test_jira_cli.py:185-211` — `snapshot_collector`
  injection pattern for poll tests (verified)
- Codebase, `tests/unit/test_ui_parity_docs.py` — docs regression test
  anchors that must not break (verified)
- Codebase, `whilly/llm_ops.py:94-95,145-146` — `_log_dir()` function and
  `mkdir(parents=True, exist_ok=True)` pattern (verified)
- Codebase, `pyproject.toml` — pytest markers, `asyncio_mode = "auto"`,
  line-length 120, target py310 (verified)

### Secondary (MEDIUM confidence)

- CONTEXT.md — decisions locked by operator; env var names `GITLAB_URL` /
  `GITLAB_TOKEN` (document source, not code)

### Tertiary (LOW confidence — see Assumptions Log)

- A1: `GITLAB_TOKEN` vs `GITLAB_API_TOKEN` env var precedence — requires
  planner decision on which to prefer
- A2: urllib vs glab CLI for GitLab HTTP ping — Claude's discretion,
  planner should choose implementation style

---

## Metadata

**Confidence breakdown:**
- Jira smoke code paths: HIGH — all existing functions verified in codebase
- GitLab credential resolution: HIGH — `_resolve_gitlab_token` verified
- GitLab HTTP client (new): MEDIUM — pattern is clear (mirror `_jira_get`) but no code exists yet
- Report file pattern: HIGH — Phase 18 evidence pattern verified line-by-line
- Docs regression: HIGH — `test_ui_parity_docs.py` verified

**Research date:** 2026-06-12
**Valid until:** 2026-07-12 (stable codebase; 30-day window)
