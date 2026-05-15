"""Single live end-to-end workflow against production services.

This is the **operator-runbook** test: it is deliberately one file, one
test, and stays dormant by default.  It hits four real systems in series
and proves the whole plan -> Jira -> Claude -> GitLab MR pipeline works.

Services touched
----------------
* **Jira** at ``https://jira.example.com`` — fetch ``DEMO-9843`` via REST v2.
* **Claude API** — the worker subprocess invokes ``claude`` (the Node
  CLI) which tunnels out via the operator's ``claudeproxy`` SSH forward
  on ``127.0.0.1:11112``.
* **GitLab** at ``https://gitlab.example.com/qa-team/e2e`` (project id
  ``218773``) — the worker pushes a branch and the ``whilly/sinks/gitlab_mr``
  sink opens an MR via the v4 API.
* **Postgres** — testcontainers ``postgres:15-alpine`` for the whilly
  control plane.  Identical to every other ``tests/conftest.py`` user.

Gate (skip, do not fail) — see ``_PREFLIGHT_SKIP_REASON`` below
--------------------------------------------------------------
1. ``WHILLY_RUN_LIVE_E2E`` != ``"1"``.
2. ``JIRA_API_TOKEN`` env var empty.
3. ``glab config get token -h gitlab.example.com`` returns empty.
4. Claude binary at ``$HOME/.reflex/.nvm/versions/node/v20.19.6/bin/claude``
   not executable.
5. ``claudeproxy`` tunnel not listening on ``127.0.0.1:11112``.
6. Docker unavailable (the shared ``postgres_dsn`` fixture takes care
   of this on its own, but we surface it via the chain for symmetry).

Each gate is a clean ``pytest.skip`` so this file contributes a single
*skipped* test (~0.5 s) to every normal pytest run.

Runtime budget
--------------
On a successful live run the test takes 5-15 minutes.  The dominant
costs are Claude bootup (~30 s), task planning (~1-2 min), and the
human-review handshake plus MR creation.  All polling loops time out
at 600 s with a clear ``pytest.fail("timed out waiting for ...")``
message so a stuck worker surfaces deterministically.

Why backend-driven (no Playwright)?
-----------------------------------
The browser-driven UI flow is already covered by ``tests/ui/``; this
test is reliability-focused — every interaction is an httpx call with
explicit timeouts and explicit auth.  No headless-chromium pixel
flake, no JS framework race, no missing-locator triage.

Cleanup
-------
The test closes the MR via PUT ``state_event=close`` and deletes the
feature branch on success.  On failure the cleanup helpers swallow
errors silently so the assertion message stays the surfaced one — the
operator can mop up any leftover MR/branch by hand from the failure log.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

# Test layout: tests/integration/test_live_full_workflow.py
REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_BIN = Path(sys.executable)

# --------------------------------------------------------------------------- #
# Constants — service endpoints + identifiers
# --------------------------------------------------------------------------- #

JIRA_SERVER_URL = "https://jira.example.com"
JIRA_KEY = "DEMO-9843"

GITLAB_HOST = "gitlab.example.com"
GITLAB_PROJECT_ID = 218773
GITLAB_HTTPS_URL = f"https://{GITLAB_HOST}/qa-team/e2e.git"
GITLAB_API_BASE = f"https://{GITLAB_HOST}/api/v4"

CLAUDE_BIN_PATH = Path.home() / ".reflex/.nvm/versions/node/v20.19.6/bin/claude"
CLAUDEPROXY_HOST = "127.0.0.1"
CLAUDEPROXY_PORT = 11112

# Worker/server tuning. Bootstrap token mirrors tests/ui/conftest.py so the
# server boots with a known cluster-join secret; the worker registers against
# it via POST /workers/register.
BOOTSTRAP_TOKEN = "ui-test-bootstrap"
SERVER_BOOT_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 600.0  # 10 minutes per poll-loop


# --------------------------------------------------------------------------- #
# Pre-flight gates — every condition collapses to pytest.skip().
# --------------------------------------------------------------------------- #


def _claudeproxy_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True iff a TCP connect to ``host:port`` succeeds within ``timeout``."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except OSError:
            return False
        return True


def _glab_gitlab_token() -> str:
    """Return the GitLab Personal Access Token from ``glab`` config (empty on miss)."""
    try:
        result = subprocess.run(  # noqa: S603 — fully controlled invocation
            ["glab", "config", "get", "token", "-h", GITLAB_HOST],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _preflight_skip_reason() -> str | None:
    """Return a human-readable skip reason, or None when every gate passes."""
    if os.environ.get("WHILLY_RUN_LIVE_E2E") != "1":
        return "gated by WHILLY_RUN_LIVE_E2E=1"
    if not (os.environ.get("JIRA_API_TOKEN") or "").strip():
        return "JIRA_API_TOKEN env var is missing or empty"
    if not _glab_gitlab_token():
        return f"glab has no token for {GITLAB_HOST}; run `glab auth login -h {GITLAB_HOST}`"
    if not (CLAUDE_BIN_PATH.is_file() and os.access(CLAUDE_BIN_PATH, os.X_OK)):
        return f"Claude binary missing or not executable at {CLAUDE_BIN_PATH}"
    if not _claudeproxy_listening(CLAUDEPROXY_HOST, CLAUDEPROXY_PORT):
        return f"claudeproxy tunnel not listening on {CLAUDEPROXY_HOST}:{CLAUDEPROXY_PORT}"
    return None


# --------------------------------------------------------------------------- #
# Helpers — port allocation, server bootup, polling, psql passthrough
# --------------------------------------------------------------------------- #


def _pick_free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http(url: str, *, timeout: float) -> None:
    """Poll ``GET <url>`` until it returns 200, or raise after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    # trust_env=False — the operator's HTTP_PROXY must not redirect loopback.
    with httpx.Client(trust_env=False, timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(url).status_code == 200:
                    return
            except Exception as exc:  # noqa: BLE001 — probe is intentionally lenient
                last_err = exc
            time.sleep(0.25)
    raise RuntimeError(f"{url!r} did not become healthy in {timeout}s: {last_err}")


def _poll_until(
    predicate: Callable[[], Any],
    *,
    description: str,
    timeout: float = POLL_TIMEOUT_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
) -> Any:
    """Call ``predicate`` every ``interval`` seconds; return its first truthy value.

    On timeout, raises ``pytest.fail`` with a message that names the
    polled predicate — the test report points the operator straight at
    the stuck step.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    last_value: Any = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except Exception as exc:  # noqa: BLE001 — pollers fail open
            last_exc = exc
            value = None
        if value:
            return value
        last_value = value
        time.sleep(interval)
    detail = f"last_value={last_value!r}"
    if last_exc is not None:
        detail += f", last_exc={type(last_exc).__name__}: {last_exc}"
    pytest.fail(f"timed out after {timeout}s waiting for: {description} ({detail})")


def _psql_run(dsn: str, sql: str) -> str:
    """Tiny psql passthrough.  Used for admin-side reads on the events table.

    Mirrors ``tests/ui/conftest.py::_psql_run`` minus the URL-parse step
    (we already have the DSN here in canonical ``postgresql://...`` shape).
    """
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    args = [
        "psql",
        "-h",
        parsed.hostname or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "postgres",
        "-d",
        (parsed.path or "/postgres").lstrip("/"),
        "-v",
        "ON_ERROR_STOP=1",
        "-X",
        "-q",
        "-A",
        "-t",
        "-c",
        sql,
    ]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _events_for_task(dsn: str, task_id: str) -> list[dict[str, Any]]:
    """Return all events.payload+event_type rows for a task, ordered oldest first.

    Reads via psql directly so the test does not depend on the
    claim-owner-gated ``GET /tasks/{task_id}/events`` endpoint — the
    operator IS the claim owner here (we are the process that registered
    the worker), but pulling JSON via the database keeps the assertion
    surface decoupled from the bearer-auth contract.
    """
    out = _psql_run(
        dsn,
        (
            "SELECT json_build_object("
            "  'event_type', event_type, "
            "  'payload', payload, "
            "  'created_at', extract(epoch from created_at)"
            ") "
            f"FROM events WHERE task_id = '{task_id}' ORDER BY id ASC"
        ),
    )
    events: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _has_event_type(events: list[dict[str, Any]], event_type: str) -> bool:
    return any(ev.get("event_type") == event_type for ev in events)


# --------------------------------------------------------------------------- #
# The test — single function, ~8 sequential steps + cleanup.
# --------------------------------------------------------------------------- #


@pytest.mark.live_e2e
@pytest.mark.skipif(
    os.environ.get("WHILLY_RUN_LIVE_E2E") != "1",
    reason="gated by WHILLY_RUN_LIVE_E2E=1",
)
def test_live_full_workflow_plan_to_real_gitlab_mr(  # noqa: PLR0915 — runbook, not refactor candidate
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    """Plan -> Jira import -> Claude worker -> human-review -> real GitLab MR."""

    # ── 0. Pre-flight gates (the marker handles WHILLY_RUN_LIVE_E2E; the rest
    # of the chain runs only when the operator has opted in). ────────────────
    skip_reason = _preflight_skip_reason()
    if skip_reason is not None:
        pytest.skip(skip_reason)

    glab_token = _glab_gitlab_token()
    jira_token = os.environ["JIRA_API_TOKEN"].strip()
    timestamp = str(int(time.time()))
    plan_id = f"live-e2e-{timestamp}"
    e2e_doc_path = f"docs/E2E-RUN-{timestamp}.md"

    # ── 1. Clone qa-team/e2e into the per-test tmp dir.  HTTPS + oauth2:<token>
    # avoids the SSH-key dance and works the same way `glab repo clone` does.
    clone_dir = tmp_path / "qa-team-e2e"
    clone_url = f"https://oauth2:{glab_token}@{GITLAB_HOST}/qa-team/e2e.git"
    subprocess.run(  # noqa: S603 — args fully controlled
        ["git", "clone", "--depth", "1", clone_url, str(clone_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # ── 2. Boot uvicorn.  Same shape as tests/ui/conftest.py::live_server.
    server_port = _pick_free_port()
    server_base_url = f"http://127.0.0.1:{server_port}"
    event_log_path = tmp_path / "whilly_events.jsonl"
    event_log_path.write_text("")
    uvicorn_log = tmp_path / "uvicorn.log"

    server_env: dict[str, str] = {
        **os.environ,
        "WHILLY_DATABASE_URL": postgres_dsn,
        "WHILLY_WORKER_BOOTSTRAP_TOKEN": BOOTSTRAP_TOKEN,
        "WHILLY_EVENT_LOG_PATH": str(event_log_path),
        "WHILLY_CSRF_ORIGIN_ALLOWLIST": f"http://127.0.0.1:{server_port},http://localhost:{server_port}",
        "WHILLY_SESSION_COOKIE_SECURE": "false",
        "PYTHONPATH": str(REPO_ROOT),
    }
    # Server hits loopback only; strip proxy vars so its httpx never tries to
    # route DB / control-plane calls through claudeproxy.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        server_env.pop(var, None)
    server_env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    server_env["no_proxy"] = server_env["NO_PROXY"]

    uvicorn_fh = uvicorn_log.open("wb")
    uvicorn_proc = subprocess.Popen(  # noqa: S603
        [
            str(PYTHON_BIN),
            "-m",
            "whilly.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(server_port),
        ],
        env=server_env,
        stdout=uvicorn_fh,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )

    worker_proc: subprocess.Popen[bytes] | None = None
    worker_log = tmp_path / "worker.log"
    worker_fh = None
    pr_number: int | None = None
    pr_branch: str | None = None
    pr_url: str | None = None
    test_failed = True  # flipped to False on success — controls log tail dump on teardown
    try:
        try:
            _wait_for_http(f"{server_base_url}/health", timeout=SERVER_BOOT_TIMEOUT_SECONDS)
        except RuntimeError:
            uvicorn_fh.flush()
            tail = uvicorn_log.read_text(encoding="utf-8", errors="replace")[-2000:]
            pytest.fail(f"uvicorn at {server_base_url} did not become healthy.\nLog tail:\n{tail}")

        # ── 3. Register the worker against the running server.  We do this
        # in-process via httpx (rather than `whilly worker register`) so we
        # can grab the plaintext bearer token without parsing CLI stdout.
        with httpx.Client(trust_env=False, timeout=15.0) as client:
            register_response = client.post(
                f"{server_base_url}/workers/register",
                headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
                json={"hostname": "live-e2e-worker"},
            )
        assert register_response.status_code == 201, (
            f"worker register failed: status={register_response.status_code} body={register_response.text}"
        )
        register_payload = register_response.json()
        worker_id = register_payload["worker_id"]
        worker_token = register_payload["token"]

        # Worker row exists in DB (sanity check on the registration side-effect).
        workers_count = _psql_run(postgres_dsn, f"SELECT COUNT(*) FROM workers WHERE id = '{worker_id}'").strip()
        assert workers_count == "1", f"workers row not found for worker_id={worker_id!r}"

        # ── 4. Login + session cookie.  POST /auth/login with a real-looking
        # email, parse the magic link from the event log, GET it to consume,
        # capture the session cookie for the subsequent CRUD calls.
        operator_email = "live-e2e@whilly.example"
        with httpx.Client(trust_env=False, timeout=15.0, follow_redirects=False) as client:
            login_response = client.post(
                f"{server_base_url}/auth/login",
                data={"email": operator_email},
            )
            assert login_response.status_code == 200, f"login form 200 expected, got {login_response.status_code}"

            # The /auth/login handler appends ``auth.magic_link.issued`` to the
            # event log when the link is freshly minted.  We bypass the SMTP-less
            # email path by reading it straight from disk.
            magic_url: str | None = None
            for line in reversed(event_log_path.read_text(encoding="utf-8").splitlines()):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event_type") == "auth.magic_link.issued" and event.get("email") == operator_email:
                    magic_url = event.get("magic_link_url")
                    break
            assert magic_url, "auth.magic_link.issued event not found in event log"

            magic_response = client.get(magic_url)
            assert magic_response.status_code == 303, (
                f"magic-link consumption expected 303, got {magic_response.status_code} body={magic_response.text[:200]}"
            )
            session_cookies = {**client.cookies}
            assert session_cookies, "no session cookie set after magic-link consumption"

        cookie_origin = f"http://127.0.0.1:{server_port}"

        # ── 5. Create the plan via the cookie-authenticated CRUD endpoint.
        # Note the Origin header — the CSRF middleware enforces an allowlist
        # match.  We seeded WHILLY_CSRF_ORIGIN_ALLOWLIST above.
        with httpx.Client(trust_env=False, timeout=15.0) as client:
            client.cookies.update(session_cookies)
            create_plan_response = client.post(
                f"{server_base_url}/api/v1/plans",
                headers={"Origin": cookie_origin},
                json={
                    "plan_id": plan_id,
                    "name": f"Live E2E {timestamp}",
                    "prd_file": "",
                    "budget_usd": 2.0,
                },
            )
        assert create_plan_response.status_code == 201, (
            f"plan create failed: {create_plan_response.status_code} body={create_plan_response.text}"
        )

        # ── 6. Import the Jira issue with a wizard override that pins the
        # mini-task to a single deterministic action.  The wizard description
        # is what the worker (Claude) actually executes — we keep it tiny so
        # the real Claude call lands in seconds rather than minutes.
        wizard_description = (
            f"Create file {e2e_doc_path} with exactly one line: "
            f"'# E2E run at {timestamp}'. Then git add the file and "
            "git commit with message 'docs: E2E live run'. Do not modify "
            "any other file."
        )
        wizard_body = {
            "jira_ref": JIRA_KEY,
            "mode": "wizard",
            "plan_id": plan_id,
            "force": True,  # plan_id is timestamped, but force keeps replays idempotent
            "wizard": {
                "description": wizard_description,
                "acceptance_criteria": [
                    f"File {e2e_doc_path} exists",
                    f"Content includes the ISO timestamp '{timestamp}'",
                ],
                "key_files": [e2e_doc_path],
                "test_steps": [f"test -f {e2e_doc_path}"],
            },
        }
        # The Jira import endpoint accepts worker bearer auth — the cookie
        # path is not wired there.  Use the freshly-minted worker bearer
        # rather than the legacy bootstrap token to exercise the per-worker
        # identity binding.
        import_response = httpx.post(
            f"{server_base_url}/api/v1/jira/import",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=wizard_body,
            timeout=60.0,
        )
        assert import_response.status_code == 201, (
            f"jira import failed: {import_response.status_code} body={import_response.text}"
        )
        import_payload = import_response.json()
        task_id = import_payload["task_id"]
        assert task_id, f"jira import returned empty task_id: {import_payload}"

        # ── 7. Boot the worker subprocess.  The worker needs:
        #   * loopback access to the server (no proxy)
        #   * outbound proxy to reach the Claude API (claudeproxy at :11112)
        #   * Direct (no proxy) access to jira/gitlab so the sinks land on
        #     the real hosts and not via claudeproxy.
        #   * cwd = the cloned qa-team/e2e directory so `git push` targets the
        #     correct remote.
        worker_env: dict[str, str] = {
            **os.environ,
            "WHILLY_CONTROL_URL": server_base_url,
            "WHILLY_PLAN_ID": plan_id,
            "WHILLY_WORKER_TOKEN": worker_token,
            "WHILLY_AUTO_OPEN_PR": "1",
            "WHILLY_PR_PROVIDER": "gitlab",
            "CLAUDE_BIN": str(CLAUDE_BIN_PATH),
            "WHILLY_MODEL": "claude-sonnet-4-6",
            "HTTP_PROXY": f"http://{CLAUDEPROXY_HOST}:{CLAUDEPROXY_PORT}",
            "HTTPS_PROXY": f"http://{CLAUDEPROXY_HOST}:{CLAUDEPROXY_PORT}",
            "NO_PROXY": ",".join(["127.0.0.1", "localhost", "::1", ".example.com", GITLAB_HOST, "jira.example.com"]),
            "JIRA_SERVER_URL": JIRA_SERVER_URL,
            "JIRA_USERNAME": os.environ.get("JIRA_USERNAME", "mvschegole"),
            "JIRA_API_TOKEN": jira_token,
            "JIRA_AUTH_SCHEME": "bearer",
            "JIRA_API_VERSION": "2",
            "JIRA_VERIFY_SSL": "false",
            "PYTHONPATH": str(REPO_ROOT),
        }
        worker_env["no_proxy"] = worker_env["NO_PROXY"]
        worker_env["http_proxy"] = worker_env["HTTP_PROXY"]
        worker_env["https_proxy"] = worker_env["HTTPS_PROXY"]
        # The worker process inherits the operator's GitLab token so the
        # gitlab_mr sink can authenticate without re-running glab.
        worker_env["GITLAB_TOKEN"] = glab_token

        worker_fh = worker_log.open("wb")
        worker_proc = subprocess.Popen(  # noqa: S603
            [str(PYTHON_BIN), "-m", "whilly.cli.worker"],
            env=worker_env,
            stdout=worker_fh,
            stderr=subprocess.STDOUT,
            cwd=str(clone_dir),
        )

        # ── 8. Wait for the worker to surface ``human_review.required``.
        # Mini-task latency on a warm Claude is normally < 60 s; we budget
        # 10 minutes because cold model bootup + plan composition can run
        # longer under load.
        def _waiting_for_review() -> bool:
            events = _events_for_task(postgres_dsn, task_id)
            return _has_event_type(events, "human_review.required")

        _poll_until(
            _waiting_for_review,
            description=f"human_review.required event on task_id={task_id}",
            timeout=POLL_TIMEOUT_SECONDS,
        )

        # ── 9. Approve via the admin endpoint.  The legacy
        # ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` fallback is intentionally NOT
        # admin-scoped (see whilly/adapters/transport/auth.py::make_admin_auth),
        # so we mint a one-shot admin bootstrap token directly via psql and
        # use it as the bearer.  This sidesteps the `whilly admin bootstrap
        # mint` CLI without weakening the auth contract — the hash matches
        # what the live admin code path expects.
        import hashlib
        import secrets as _secrets

        admin_plaintext = _secrets.token_urlsafe(32)
        admin_hash = hashlib.sha256(admin_plaintext.encode("utf-8")).hexdigest()
        _psql_run(
            postgres_dsn,
            (
                "INSERT INTO bootstrap_tokens "
                "(token_hash, owner_email, expires_at, is_admin) "
                f"VALUES ('{admin_hash}', 'live-e2e-admin@whilly.example', NULL, true)"
            ),
        )
        approve_response = httpx.post(
            f"{server_base_url}/api/v1/tasks/{task_id}/human-review",
            headers={"Authorization": f"Bearer {admin_plaintext}"},
            json={
                "decision": "approved",
                "reviewer": "live-e2e@whilly",
                "comment": "Approved by live E2E test",
            },
            timeout=15.0,
        )
        assert approve_response.status_code == 200, (
            f"human-review approval failed: {approve_response.status_code} body={approve_response.text}"
        )

        # ── 10. Wait for the worker to finish and open the MR.  We watch for
        # BOTH the DONE marker (`pr.opened` is only emitted after the task
        # transitions DONE) and the `pr.opened` audit event itself.
        def _waiting_for_pr_opened() -> dict[str, Any] | None:
            events = _events_for_task(postgres_dsn, task_id)
            for ev in events:
                if ev.get("event_type") == "pr.opened":
                    return ev
            return None

        pr_event = _poll_until(
            _waiting_for_pr_opened,
            description=f"pr.opened event on task_id={task_id}",
            timeout=POLL_TIMEOUT_SECONDS,
        )
        payload = pr_event.get("payload") or {}
        pr_url = payload.get("pr_url")
        pr_branch = payload.get("branch")
        pr_number_raw = payload.get("pr_number")
        assert pr_url and pr_branch and pr_number_raw, f"pr.opened payload missing fields: {payload!r}"
        pr_number = int(pr_number_raw)

        # ── 11. Verify the MR exists on GitLab via the v4 API.  This is the
        # acceptance pin — everything before this is "internal events say so";
        # this is "the real GitLab project agrees".
        mr_response = httpx.get(
            f"{GITLAB_API_BASE}/projects/{GITLAB_PROJECT_ID}/merge_requests/{pr_number}",
            headers={"PRIVATE-TOKEN": glab_token},
            timeout=30.0,
            verify=False,  # noqa: S501 — corp PKI; matches glab's own default
        )
        assert mr_response.status_code == 200, (
            f"GitLab MR lookup failed: {mr_response.status_code} body={mr_response.text}"
        )
        mr_payload = mr_response.json()
        assert mr_payload.get("state") == "opened", (
            f"expected state=opened, got {mr_payload.get('state')!r}; full body={mr_payload}"
        )
        assert mr_payload.get("web_url") == pr_url, (
            f"web_url mismatch: API={mr_payload.get('web_url')!r}, pr.opened payload={pr_url!r}"
        )

        test_failed = False
    finally:
        # ── Cleanup ──────────────────────────────────────────────────────
        #
        # Close the MR + delete its source branch on success so we don't
        # accumulate open MRs in qa-team/e2e.  All cleanup errors are swallowed
        # — a failing test must still surface its original assertion message.

        if pr_number is not None:
            try:
                httpx.put(
                    f"{GITLAB_API_BASE}/projects/{GITLAB_PROJECT_ID}/merge_requests/{pr_number}",
                    headers={"PRIVATE-TOKEN": glab_token},
                    json={"state_event": "close"},
                    timeout=15.0,
                    verify=False,  # noqa: S501
                )
            except Exception:  # noqa: BLE001 — best effort
                pass
        if pr_branch:
            try:
                httpx.delete(
                    f"{GITLAB_API_BASE}/projects/{GITLAB_PROJECT_ID}/repository/branches/{pr_branch}",
                    headers={"PRIVATE-TOKEN": glab_token},
                    timeout=15.0,
                    verify=False,  # noqa: S501
                )
            except Exception:  # noqa: BLE001 — best effort
                pass

        # Terminate worker first (it might still be polling the server).
        if worker_proc is not None:
            worker_proc.terminate()
            try:
                worker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker_proc.kill()
                worker_proc.wait(timeout=5)
        if worker_fh is not None:
            worker_fh.close()

        uvicorn_proc.terminate()
        try:
            uvicorn_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            uvicorn_proc.kill()
            uvicorn_proc.wait(timeout=2)
        uvicorn_fh.close()

        # If the test failed, dump the tail of each subprocess log to stderr
        # so the operator does not have to dig through tmp_path by hand.
        if test_failed:
            for label, path in (("uvicorn", uvicorn_log), ("worker", worker_log)):
                if not path.exists():
                    continue
                tail = path.read_text(encoding="utf-8", errors="replace")[-3000:]
                print(f"\n── {label} log tail ({path}) ──\n{tail}\n", file=sys.stderr)
