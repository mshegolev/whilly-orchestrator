---
phase: 19-live-authenticated-smoke
reviewed: 2026-06-12T00:00:00Z
depth: standard
files_reviewed: 9
files_reviewed_list:
  - whilly/cli/smoke.py
  - whilly/cli/gitlab.py
  - whilly/cli/jira.py
  - whilly/cli/__init__.py
  - tests/unit/cli/test_smoke.py
  - tests/unit/cli/test_jira_smoke.py
  - tests/unit/cli/test_gitlab_smoke.py
  - tests/unit/test_docs_live_smoke.py
  - docs/Whilly-Usage.md
findings:
  critical: 3
  warning: 9
  info: 6
  total: 18
status: issues_found
---

# Phase 19: Code Review Report

**Reviewed:** 2026-06-12
**Depth:** standard
**Files Reviewed:** 9
**Status:** issues_found

## Summary

Reviewed the live-authenticated-smoke surface: shared `SmokeReport` foundation
(`whilly/cli/smoke.py`), the new `whilly gitlab` group (`whilly/cli/gitlab.py`),
the `jira smoke` subcommand (`whilly/cli/jira.py` — new code only), CLI
dispatcher registration, four test files, and the docs section. All 30 phase
tests pass locally; lint conventions are followed.

The architecture (injectable getter/collector, secret-free payload composition,
config gate before any network call) is sound. However, three of the phase's
**locked security/honesty decisions are violated** in the implementation:

1. A real token-leakage path exists in `_gitlab_get` error messages when
   `GITLAB_URL` carries embedded credentials (CR-01).
2. `gitlab smoke --persist` is a stub that validates the DSN and persists
   nothing (CR-02).
3. Three of the six advertised jira checks (`comments`, `changelog`,
   `remote_links`) are tautologies that can never fail independently —
   fabricated pass flags in an audit artifact (CR-03).

Additional warnings cover an uncaught-exception path that would print a raw
traceback, exit-code contract corruption around `--persist`, a hardcoded
corporate host, URL-parsing defects, a docs/code default mismatch, and
contradictory secret-leak test invariants.

## Critical Issues

### CR-01: GitLab token can leak into report JSON and stdout via `_gitlab_get` error messages

**File:** `whilly/cli/gitlab.py:160,164,166` (origin), `whilly/cli/gitlab.py:271,297-299` (sink)
**Issue:** `_gitlab_get` embeds the raw request URL in every `RuntimeError` it
raises (`f"GitLab GET {url!r} failed: ..."`). The URL is built from the
`GITLAB_URL` env var **verbatim** (`_resolve_gitlab_config_state`, line 110 —
only trailing slash is stripped). If an operator sets
`GITLAB_URL=https://oauth2:glpat-XXXX@gitlab.example.com` (a common CI/clone
pattern), urllib will fail to connect (userinfo in netloc), and the exception
text — containing the full credentialed URL — flows into the check hint via
`hint=f"... Detail: {exc}"` (lines 271 and 297-299), which is then written to
the report JSON file under `whilly_logs/smoke/` and printed to stdout in the
human summary (line 375). The hint sites carefully call `_redact_url(url)` for
the URL they format themselves, but the unredacted URL inside `{exc}` bypasses
redaction entirely. This violates the locked decision "tokens/secrets never in
reports, stdout, or DB events." The existing no-secrets test
(`test_gitlab_smoke_report_contains_no_secrets`) only covers the all-pass path
with a clean `GITLAB_URL`, so it cannot catch this.
**Fix:**
```python
# in _gitlab_get — redact the URL in every error message:
safe_url = _redact_url(url)
...
raise RuntimeError(f"GitLab GET {safe_url!r} failed: HTTP {exc.code} — {body}") from exc
...
raise RuntimeError(f"GitLab GET {safe_url!r} network error: {exc.reason}") from exc
# and harden at the source — strip userinfo when resolving config:
url = _redact_url((env.get("GITLAB_URL") or env.get("WHILLY_GITLAB_URL") or "").strip().rstrip("/"))
```
Add a regression test: failing getter whose exception message contains a
credentialed URL must not surface the credential in the report file.

### CR-02: `whilly gitlab smoke --persist` silently persists nothing

**File:** `whilly/cli/gitlab.py:346-355`
**Issue:** The `--persist` flag is advertised in the parser help as "Persist
smoke event to Postgres (requires WHILLY_DATABASE_URL)" and in
`docs/Whilly-Usage.md:714-716` as appending a DB audit event. The
implementation only validates that `WHILLY_DATABASE_URL` is set and then does
**nothing** — there is no call to any persist function (compare
`_persist_smoke_event` in `jira.py:854`). An operator running
`whilly gitlab smoke --persist` with a DSN configured gets exit 0 and believes
an audit event was written; none was. This is incorrect behavior in
evidence/audit tooling.
**Fix:** Either implement persistence (mirror `jira.py`'s
`asyncio.run(_persist_smoke_event(dsn=dsn, payload=payload))` with
`issue_key` replaced by a repo identifier), or remove the `--persist` flag
from the gitlab parser and docs until it exists. Do not ship a flag that
validates config and then no-ops.

### CR-03: `comments`, `changelog`, `remote_links` checks are tautologies — fabricated pass flags

**File:** `whilly/cli/jira.py:785-807`
**Issue:** `JiraWorkSnapshot` declares non-Optional tuple fields
(`whilly/jira_watch.py:39-41`), so once a snapshot exists:
- `passed=snapshot.comments is not None` (line 788) is always `True`;
- `passed=len(snapshot.changelog_ids) >= 0` (line 793) is **tautologically**
  `True` for any sequence — this expression cannot evaluate to `False`;
- `passed=snapshot.links is not None` (line 798) is always `True`.

Three of the six advertised checks can therefore never fail independently of
`issue_fetch` — they verify nothing, yet are recorded as individually "passed"
in the evidence report and counted in `passed=6/6`. This directly violates the
phase's locked decision on honest per-check accumulation (no fabricated pass
flags), and the docs claim ("All six checks run... so you get a full picture")
overstates what is verified. Only `auth`, `issue_fetch`, and `classify` carry
real signal.
**Fix:** Make each check assert something falsifiable, e.g.:
```python
# comments: the comment endpoint returned a parseable list (collector already
# fetched it separately — surface that success/failure distinctly), or at
# minimum validate shape:
report.add_check("comments", passed=all(isinstance(c, dict) and c.get("id") for c in snapshot.comments) , ...)
# changelog: expand=changelog was honoured — e.g. the snapshot exposes whether
# the 'changelog' key existed in the issue payload (extend the collector), not len() >= 0.
```
If the underlying data cannot distinguish these outcomes, collapse them into
`issue_fetch` rather than reporting separate always-green checks.

## Warnings

### WR-01: Unexpected exception types from the collector escape as raw tracebacks

**File:** `whilly/cli/jira.py:772-783`
**Issue:** The except clause catches `(OSError, RuntimeError, ValueError,
json.JSONDecodeError)` (note: `JSONDecodeError` is already a `ValueError`
subclass — redundant). `collect_jira_work_snapshot` →
`jira_work_snapshot_from_payloads` can raise `KeyError`
(`metadata["classification"]`, jira_watch.py:122), `TypeError`/`AttributeError`
on malformed Jira payload shapes (ADF flattening, changelog iteration). Any
such exception propagates out of `_run_jira_smoke` and prints a raw Python
traceback — violating the locked decision "no raw tracebacks to operators."
**Fix:** Append a final guard:
```python
except Exception as exc:  # noqa: BLE001 — operator-facing CLI must not traceback
    hint = f"Unexpected error while fetching {issue_key}: {exc.__class__.__name__}: {exc}"
    report.add_check("auth", passed=False, hint=hint)
    report.add_check("issue_fetch", passed=False, hint=hint)
```
(keeping the narrower clause first if differentiated hints are wanted).

### WR-02: `--persist` gates read `os.environ` instead of the injected environment

**File:** `whilly/cli/jira.py:826`, `whilly/cli/gitlab.py:347-349`
**Issue:** Both smoke implementations accept an injectable `environ`/`env`
mapping and use it for all credential resolution, but the
`WHILLY_DATABASE_URL` lookup bypasses it and reads the real `os.environ`. The
persist path is therefore untestable through the DI seam (no phase test covers
it), and behavior diverges between injected and real environments.
**Fix:** `dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()` (jira)
and `env.get(...)` (gitlab).

### WR-03: `--persist` corrupts the documented exit-code contract

**File:** `whilly/cli/jira.py:825-834`, `whilly/cli/gitlab.py:346-355`, `docs/Whilly-Usage.md:694-716`
**Issue:** Docs define exit 1 = "one or more checks failed", exit 2 =
"configuration missing", and describe `--persist` as "best-effort". The code:
(a) returns 2 when `--persist` is given without a DSN **even when all checks
passed** — and in gitlab this `return` (line 355) happens before the summary
is printed, so the operator sees the check results only in the file;
(b) jira returns 1 on a persist exception (line 834) even when all smoke
checks passed, masking a green smoke run as a check failure. Both contradict
the "best-effort" documentation and make exit codes ambiguous for CI
consumers.
**Fix:** Pick one contract. If best-effort: log the persist failure to stderr
and return the smoke result's exit code. If hard requirement: update the docs
and move the DSN gate before any checks run so exit 2 means what it says.

### WR-04: Hardcoded corporate host as fallback

**File:** `whilly/cli/gitlab.py:240`
**Issue:** `host = _extract_host_from_url(args.repo_url) or "gitlab.services.mts.ru"`
bakes an environment-specific corporate hostname into generic open code. On
any other deployment, a malformed `--repo-url` silently sends the `glab
config get token -h gitlab.services.mts.ru` lookup to an unrelated host and
may resolve the wrong token.
**Fix:** Fall back to the host of the resolved `GITLAB_URL`
(`urllib.parse.urlsplit(url).hostname`), or treat an unextractable host as a
config error (exit 2) with a clear message.

### WR-05: `_extract_host_from_url` mis-parses URLs containing userinfo

**File:** `whilly/cli/gitlab.py:46,174-177`
**Issue:** The regex `(?:https?://|git@)([^/:]+)` captures up to the first
`/` or `:`. For `https://user:pass@host/group/repo` it captures `user`; for
`https://user@host/group/repo` it captures `user@host`. The wrong "host" is
then passed to the `glab config get token -h <host>` fallback, which will
look up a token for a nonexistent host (silently yielding no token → exit 2,
or potentially a token for the wrong configured alias).
**Fix:** Use the stdlib parser:
```python
def _extract_host_from_url(repo_url: str) -> str:
    try:
        host = urllib.parse.urlsplit(repo_url).hostname
    except Exception:
        host = None
    if host:
        return host
    m = re.match(r"git@([^:/]+)", repo_url)
    return m.group(1) if m else ""
```

### WR-06: SSH-style repo URLs accepted by host extraction but broken by project-path resolution

**File:** `whilly/cli/gitlab.py:180-201`
**Issue:** `_extract_host_from_url` explicitly supports `git@host:group/repo`
forms, but `_resolve_project_path` does not: `urlsplit("git@host:group/repo.git")`
yields the entire string as `path`, producing the encoded project path
`git%40host%3Agroup%2Frepo`, so `project_access` is guaranteed to fail with a
misleading "verify the repo path" hint mentioning `git@host:group/repo`. The
two helpers disagree about what input is valid.
**Fix:** Either reject non-HTTP(S) `--repo-url` values up front with a clear
error ("pass the https:// repository URL"), or convert the SSH form
(`git@host:path` → `path`) before encoding.

### WR-07: Docs state `--timeout` default is 30; code default is 15

**File:** `docs/Whilly-Usage.md:665` vs `whilly/cli/jira.py:304`, `whilly/cli/gitlab.py:74-78`
**Issue:** The Optional flags table documents "Per-request timeout in seconds
(default 30)" while both parsers default to 15. Operators tuning for slow
corporate Jira instances will be misled.
**Fix:** Change the docs to 15 (or the code to 30 — pick one and keep them in
sync). Note the docs flag table also only exists for jira; gitlab's
`--timeout/--persist/--json` flags are undocumented.

### WR-08: Secret-leak tests assert an invariant the failure path violates

**File:** `tests/unit/cli/test_jira_smoke.py:262`, `tests/unit/cli/test_gitlab_smoke.py:221` vs `whilly/cli/jira.py:781,810`, `whilly/cli/gitlab.py:271`
**Issue:** `test_jira_smoke_report_contains_no_token_or_dsn` asserts the
literal string `"JIRA_API_TOKEN"` must never appear in a report, and the
gitlab counterpart asserts the same for `"GITLAB_TOKEN"`. But every
**failure-path** hint deliberately embeds those env-var names ("Check
JIRA_SERVER_URL, JIRA_API_TOKEN, ...", "Verify GITLAB_TOKEN is valid for ..."),
and the failure-path tests (test_jira_smoke.py:160) explicitly accept them.
The "no token-literal" invariant therefore only holds on the all-pass path
that those tests happen to exercise — the test names promise a guarantee the
implementation does not provide, giving false security confidence. Env-var
*names* are not secrets; the assertions conflate name and value.
**Fix:** Drop the env-var-name assertions (keep the token-*value* and DSN
assertions), or add a failure-path variant making the intended invariant
explicit. The contradiction must be resolved one way or the other.

### WR-09: Durations are fabricated zeros; `add_timed_check` is dead code

**File:** `whilly/cli/smoke.py:115-124`, `docs/Whilly-Usage.md:711`
**Issue:** `SmokeReport.add_timed_check` is never called anywhere in the
codebase; every check is recorded via `add_check` with a hardcoded
`duration_seconds: 0.0`. The docs claim reports contain "per-check pass/fail
results, durations" — the duration data in every shipped report is a
fabricated 0.0.
**Fix:** Either time the checks (wrap each check in
`time.monotonic()` and use `add_timed_check`) or remove the
`duration_seconds` field, the unused method, and the docs claim.

## Info

### IN-01: GitLab `auth` check does not validate the response shape

**File:** `whilly/cli/gitlab.py:264-266`
**Issue:** The `/api/v4/user` response is discarded; any 200 JSON body
(including `{}` from a misbehaving proxy) passes the auth check, while
`project_access` correctly validates `data.get("id")`.
**Fix:** `passed=bool(data.get("id") or data.get("username"))`.

### IN-02: Exit-code 2 is overloaded (usage error vs config missing vs bad --issue)

**File:** `whilly/cli/gitlab.py:402-405`, `whilly/cli/jira.py:733-741`
**Issue:** argparse usage errors (SystemExit 2) and a malformed `--issue`
value both surface as exit 2, which the docs define strictly as
"Configuration missing (env vars not set)". CI consumers cannot distinguish
misuse from missing credentials.
**Fix:** Document the overlap, or use a distinct message/code for validation
errors.

### IN-03: Report-path output channel is inconsistent between the two commands

**File:** `whilly/cli/gitlab.py:378`, `whilly/cli/jira.py:849`
**Issue:** gitlab prints `report: <path>` to **stderr** in all modes; jira
prints `report=<path>` to **stdout** and only in non-`--json` mode (in jira
`--json` mode the path is not shown at all). Also note differing key formats
(`report:` vs `report=`).
**Fix:** Pick one convention (stderr in all modes is the safer choice for
`--json` consumers) and apply it to both.

### IN-04: Report filename embeds ISO timestamp with colons

**File:** `whilly/cli/smoke.py:176`
**Issue:** `jira-smoke-2026-06-12T10:30:00.123456Z.json` contains `:` —
illegal on Windows/NTFS and awkward in some archive tools. POSIX-only today,
but it's a gratuitous portability trap.
**Fix:** `ts.replace(":", "")` (or `%Y%m%dT%H%M%SZ`) for the filename while
keeping the full ISO timestamp inside the payload.

### IN-05: `target_host` is empty when Jira config comes from whilly.toml

**File:** `whilly/cli/jira.py:765-766`
**Issue:** The config gate accepts `server_url` from the `[jira]` section of
`whilly.toml`, but `target_host` is derived only from the
`JIRA_SERVER_URL`/`WHILLY_JIRA_SERVER_URL` env vars — reports then carry
`"target_host": ""` despite a successful run against a real host.
**Fix:** Reuse `_jira_config_state(effective_config_reader(), effective_env).server_url`
for the redacted target.

### IN-06: `target_host`/`target.host` contain the full base URL, not a hostname

**File:** `whilly/cli/jira.py:766`, `whilly/cli/gitlab.py:337`, `docs/Whilly-Usage.md:711-712`
**Issue:** `_redact_url` returns the full URL minus userinfo
(`https://host/path`), but the report keys are named `host` and the docs
promise "hostname only — no ... full URLs". No secret risk (post CR-01 fix),
but the field misrepresents its content.
**Fix:** Use `urllib.parse.urlsplit(url).hostname` for these fields, or
rename the keys/docs to `base_url`.

---

_Reviewed: 2026-06-12_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
