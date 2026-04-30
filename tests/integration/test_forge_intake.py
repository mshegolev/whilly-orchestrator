"""Integration tests for ``whilly forge intake`` (TASK-108a).

Drives the Forge intake CLI surface against a real Postgres
(testcontainers) with the ``gh`` subprocess monkeypatched at the
:mod:`whilly.forge._gh` seam (``_run_gh``). Exercises:

* VAL-FORGE-003 — pinned ``--json`` field set on ``gh issue view``.
* VAL-FORGE-004 — plan row carries ``github_issue_ref``.
* VAL-FORGE-006 — combined ``gh issue edit`` invocation with both
  ``--add-label`` and ``--remove-label`` flags.
* VAL-FORGE-007 — idempotent re-run returns the existing plan id and
  does **not** re-invoke ``gh issue edit``.
* VAL-FORGE-008 — ``gh`` absent → exit 2, install hint on stderr.
* VAL-FORGE-009 — issue not found → exit 1, no plan written.
* VAL-FORGE-010 — network/transport error from ``gh`` → graceful
  failure, no partial plan.
* VAL-FORGE-011 — plan has either ``prd_file`` set or zero tasks
  (every plan covers one branch of the disjunction).
* VAL-FORGE-012 — ``GET /api/v1/plans/<id>`` exposes
  ``github_issue_ref`` in the response.
* VAL-FORGE-014 — env passed to ``gh`` is the resolver's output
  (``gh_subprocess_env``).
* VAL-FORGE-016 — ``--help`` mentions issue ref shape, label
  transition, and ``gh`` requirement.
* VAL-FORGE-017 — malformed input is rejected before any
  ``subprocess.run`` call.
* VAL-FORGE-018 — failure post-fetch does NOT flip the label.
* VAL-CROSS-001 — ``plan.created`` event reaches the events table
  via the lifespan flusher.

Tests use the function-scoped ``db_pool`` fixture so each scenario
runs against a TRUNCATEd schema.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.forge import _gh as forge_gh
from whilly.forge import intake as forge_intake

pytestmark = DOCKER_REQUIRED


REPO_ROOT: Path = Path(__file__).resolve().parents[2]
FAKE_CLAUDE_PRD: Path = REPO_ROOT / "tests" / "fixtures" / "fake_claude_prd.sh"


# ── Helpers: canned gh payloads ───────────────────────────────────────────
def _canned_issue_payload(number: int) -> dict[str, Any]:
    """Stable issue payload used by the happy-path tests."""
    return {
        "number": number,
        "title": "[mission-test] forge intake smoke",
        "body": "Forge intake should turn this issue into a Whilly plan.",
        "labels": [{"name": "whilly-pending"}],
        "comments": [
            {"body": "Comment 1: extra context for the PRD wizard."},
        ],
        "state": "OPEN",
        "url": f"https://github.com/example/repo/issues/{number}",
    }


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


async def _run_intake(argv: list[str], **kwargs: Any) -> int:
    """Run :func:`forge_intake.run_forge_intake_command` in a worker thread.

    The CLI uses ``asyncio.run`` internally for its DB round-trips,
    which would explode if invoked from inside the pytest-asyncio
    event loop. ``asyncio.to_thread`` runs the synchronous CLI in a
    thread so tests can stay ``async def`` and continue to use the
    async ``db_pool`` fixture for assertions.
    """
    return await asyncio.to_thread(
        forge_intake.run_forge_intake_command,
        argv,
        **kwargs,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_db(db_pool: asyncpg.Pool) -> None:
    """Force the autouse db_pool fixture so each test gets a TRUNCATEd schema."""
    return None


@pytest.fixture
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each test in a tmp cwd so PRD files don't pollute the repo."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    return tmp_path


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``CLAUDE_BIN`` at the PRD-aware stub for headless intake runs."""
    assert FAKE_CLAUDE_PRD.exists(), f"fixture missing: {FAKE_CLAUDE_PRD}"
    assert os.access(FAKE_CLAUDE_PRD, os.X_OK), f"fixture lost its executable bit: {FAKE_CLAUDE_PRD}"
    monkeypatch.setenv("CLAUDE_BIN", str(FAKE_CLAUDE_PRD))
    return FAKE_CLAUDE_PRD


@pytest.fixture
def database_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set ``WHILLY_DATABASE_URL`` for the duration of the test."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", postgres_dsn)
    return postgres_dsn


@pytest.fixture
def gh_recorder(monkeypatch: pytest.MonkeyPatch):
    """Record every ``forge._gh._run_gh`` invocation; return the call list.

    Tests append (or pre-load) a list of canned ``CompletedProcess``
    results onto the recorder; subsequent calls pop in order. The
    captured argv list is exposed as ``recorder.calls`` so tests can
    assert the exact ``gh`` invocations.
    """

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.envs: list[dict[str, str]] = []
            self.responses: list[subprocess.CompletedProcess[str]] = []

        def queue(self, response: subprocess.CompletedProcess[str]) -> None:
            self.responses.append(response)

        def __call__(
            self,
            args: list[str],
            *,
            timeout: float = forge_gh.DEFAULT_GH_TIMEOUT_SECONDS,
        ) -> subprocess.CompletedProcess[str]:
            del timeout
            from whilly.gh_utils import gh_subprocess_env

            # Mirror the production helper's gh-on-PATH precondition so
            # tests asserting the "gh CLI absent" path get the same
            # exception type.
            if shutil.which("gh") is None:
                raise forge_gh.GHCLIMissingError(
                    "gh CLI is not on PATH; install via `brew install gh` or see https://cli.github.com."
                )
            self.calls.append(list(args))
            self.envs.append(dict(gh_subprocess_env()))
            if not self.responses:
                raise AssertionError(f"gh_recorder: no canned response queued for invocation {args!r}")
            return self.responses.pop(0)

    rec = _Recorder()
    monkeypatch.setattr(forge_gh, "_run_gh", rec)
    return rec


# ── VAL-FORGE-016: --help discoverability ────────────────────────────────
def test_intake_help_documents_issue_ref_and_labels(capsys: pytest.CaptureFixture[str]) -> None:
    """--help mentions ``owner/repo``, the label transition and ``gh``."""
    rc = forge_intake.run_forge_intake_command(["--help"])
    captured = capsys.readouterr()
    assert rc == forge_intake.EXIT_OK
    out_lower = captured.out.lower()
    # (a) issue ref shape mentioned.
    assert "owner/repo" in out_lower
    # (b) label transition documented (both literals).
    assert "whilly-pending" in out_lower
    assert "whilly-in-progress" in out_lower
    # (c) gh CLI requirement called out.
    assert "gh" in out_lower


# ── VAL-FORGE-017: malformed input rejected before subprocess ────────────
@pytest.mark.parametrize(
    "bad_ref",
    [
        "garbage",
        "owner/repo",
        "owner/repo/abc",
        "owner//42",
        "/repo/42",
    ],
)
def test_malformed_issue_ref_rejected_without_subprocess(
    bad_ref: str,
    gh_recorder,
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad shape exits 1; ``gh`` is never invoked."""
    rc = forge_intake.run_forge_intake_command([bad_ref])
    captured = capsys.readouterr()
    assert rc == forge_intake.EXIT_USER_ERROR
    assert "owner/repo" in captured.err
    assert gh_recorder.calls == []


# ── VAL-FORGE-008: gh absent ─────────────────────────────────────────────
def test_gh_cli_missing_returns_environment_error(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``shutil.which('gh')`` returning None → exit 2, install hint."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    rc = forge_intake.run_forge_intake_command(["owner/repo/123"])
    captured = capsys.readouterr()
    assert rc == forge_intake.EXIT_ENVIRONMENT_ERROR
    err_lower = captured.err.lower()
    assert "gh" in err_lower
    # Either the brew install command or the cli.github.com link.
    assert "install" in err_lower or "cli.github.com" in err_lower


# ── VAL-FORGE-009: issue not found ───────────────────────────────────────
async def test_issue_not_found_exits_user_error_no_plan(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gh exit 1 + GraphQL miss → exit 1; no plan written."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    gh_recorder.queue(
        _completed(
            stdout="",
            stderr="GraphQL: Could not resolve to an Issue or Pull Request",
            returncode=1,
        )
    )
    rc = await _run_intake(["owner/repo/999999"])
    captured = capsys.readouterr()
    assert rc == forge_intake.EXIT_USER_ERROR
    assert "issue not found" in captured.err.lower()
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval("SELECT count(*) FROM plans")
    assert plan_count == 0


# ── VAL-FORGE-010: network/transport error ───────────────────────────────
async def test_network_error_graceful_failure_no_partial_plan(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gh exit non-zero with network signature → exit 1; no plan."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    gh_recorder.queue(
        _completed(
            stdout="",
            stderr="could not connect to api.github.com: dial tcp: lookup api.github.com: no such host",
            returncode=1,
        )
    )
    rc = await _run_intake(["owner/repo/123"])
    captured = capsys.readouterr()
    assert rc != 0
    assert "could not connect" in captured.err.lower()
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval("SELECT count(*) FROM plans")
    assert plan_count == 0


# ── Happy path: plan creation + label flip + DB row + event ──────────────
async def test_intake_happy_path_creates_plan_and_flips_label(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful intake: plan row, github_issue_ref populated, label flipped exactly once."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    # Queue: (1) gh issue view → canned payload, (2) gh issue edit → ok.
    import json as _json

    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(123))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/123"))

    rc = await _run_intake(["owner/repo/123"])
    captured = capsys.readouterr()
    assert rc == forge_intake.EXIT_OK, captured.err

    # VAL-FORGE-003 — first call shape: gh issue view <N> --repo OWNER/REPO --json <fields>.
    assert len(gh_recorder.calls) == 2
    view_argv = gh_recorder.calls[0]
    assert view_argv[0] == "issue"
    assert view_argv[1] == "view"
    assert view_argv[2] == "123"
    assert "--repo" in view_argv
    assert view_argv[view_argv.index("--repo") + 1] == "owner/repo"
    assert "--json" in view_argv
    json_fields_value = view_argv[view_argv.index("--json") + 1]
    json_fields = set(json_fields_value.split(","))
    expected_fields = set(forge_gh.GH_ISSUE_VIEW_JSON_FIELDS.split(","))
    assert expected_fields.issubset(json_fields), (
        f"Pinned --json fields {expected_fields} not all present in {json_fields}"
    )

    # VAL-FORGE-006 — second call shape: gh issue edit <N> --repo OWNER/REPO
    # --remove-label whilly-pending --add-label whilly-in-progress.
    edit_argv = gh_recorder.calls[1]
    assert edit_argv[0] == "issue"
    assert edit_argv[1] == "edit"
    assert edit_argv[2] == "123"
    assert "--remove-label" in edit_argv
    assert edit_argv[edit_argv.index("--remove-label") + 1] == "whilly-pending"
    assert "--add-label" in edit_argv
    assert edit_argv[edit_argv.index("--add-label") + 1] == "whilly-in-progress"

    # VAL-FORGE-004 — exactly one plans row carries the canonical ref.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, github_issue_ref FROM plans WHERE github_issue_ref = $1",
            "owner/repo/123",
        )
    assert len(rows) == 1
    assert rows[0]["github_issue_ref"] == "owner/repo/123"

    # VAL-CROSS-001 / VAL-CROSS-053 — exactly one ``plan.created`` event row
    # is emitted with payload carrying the canonical issue ref. Defence in
    # depth: every successful intake should now have this event (added as
    # part of the M3 r1 scrutiny unblocker for task-108a).
    async with db_pool.acquire() as conn:
        plan_id = rows[0]["id"]
        event_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            plan_id,
        )
        assert event_count == 1
        ref_value = await conn.fetchval(
            "SELECT payload->>'github_issue_ref' FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            plan_id,
        )
        assert ref_value == "owner/repo/123"
        # Diagnostic payload fields are present per the implementation
        # contract (name, tasks_count) so downstream observers can render
        # a human-readable line without re-querying ``plans``/``tasks``.
        name_value = await conn.fetchval(
            "SELECT payload->>'name' FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            plan_id,
        )
        assert isinstance(name_value, str) and name_value
        tasks_count_value = await conn.fetchval(
            "SELECT (payload->>'tasks_count')::int FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            plan_id,
        )
        assert isinstance(tasks_count_value, int) and tasks_count_value >= 0


# ── VAL-FORGE-007: idempotent re-run ─────────────────────────────────────
async def test_intake_idempotent_re_run_no_duplicate_no_extra_label_call(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-running the same intake returns the existing plan id; no extra gh edit."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    import json as _json

    # First run — view + edit.
    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(123))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/123"))

    rc1 = await _run_intake(["owner/repo/123"])
    capsys.readouterr()
    assert rc1 == forge_intake.EXIT_OK

    first_calls = list(gh_recorder.calls)

    # Second run — same args. No gh invocation should occur (idempotent
    # short-circuit hits before fetch_issue).
    rc2 = await _run_intake(["owner/repo/123"])
    captured2 = capsys.readouterr()
    assert rc2 == forge_intake.EXIT_OK

    # No new calls were queued; assert the recorder didn't pop any.
    assert gh_recorder.calls == first_calls

    # Stdout from the second run includes the existing plan id message.
    assert "already exists" in captured2.out

    # Exactly one row in the DB.
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval(
            "SELECT count(*) FROM plans WHERE github_issue_ref = $1",
            "owner/repo/123",
        )
    assert plan_count == 1


# ── VAL-FORGE-014: env passed to gh is gh_subprocess_env() output ────────
async def test_intake_uses_gh_subprocess_env(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    gh_recorder,
) -> None:
    """The captured env for ``gh`` matches ``gh_subprocess_env()``."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    monkeypatch.setenv("WHILLY_GH_TOKEN", "fake-token-for-test")
    import json as _json

    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(124))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/124"))

    rc = await _run_intake(["owner/repo/124"])
    assert rc == forge_intake.EXIT_OK

    # Both invocations should carry the resolved token.
    from whilly.gh_utils import gh_subprocess_env

    expected = gh_subprocess_env()
    assert expected["GITHUB_TOKEN"] == "fake-token-for-test"
    for env in gh_recorder.envs:
        assert env["GITHUB_TOKEN"] == "fake-token-for-test"


# ── VAL-FORGE-018: PRD failure post-fetch does NOT flip label ────────────
async def test_failed_prd_after_fetch_does_not_flip_label(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PRD generation raising → no plan, no ``gh issue edit`` invocation."""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    import json as _json

    # Only queue the view call; the edit call must never happen.
    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(125))))

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("boom: PRD generator failed")

    rc = await _run_intake(
        ["owner/repo/125"],
        prd_runner=_boom,
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "PRD generation failed" in captured.err

    # gh issue edit must not have been invoked (only the view from the queue).
    assert len(gh_recorder.calls) == 1
    assert gh_recorder.calls[0][0:2] == ["issue", "view"]

    # No plan inserted.
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval(
            "SELECT count(*) FROM plans WHERE github_issue_ref = $1",
            "owner/repo/125",
        )
    assert plan_count == 0


# ── VAL-FORGE-011: every plan has prd_file OR zero tasks ─────────────────
async def test_intake_plan_has_prd_or_zero_tasks(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """A successful intake produces a PRD file on disk; tasks ≥ 0 is acceptable.

    The fake_claude_prd.sh stub emits one task — so this concrete
    happy-path lands with prd_file existing AND ≥ 1 task. The
    contract (VAL-FORGE-011) says ``prd_file IS NOT NULL AND
    Path(prd_file).exists()`` OR ``tasks_count == 0`` — we assert the
    happy-path branch.
    """
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    import json as _json

    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(126))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/126"))

    rc = await _run_intake(["owner/repo/126"])
    assert rc == forge_intake.EXIT_OK

    # PRD file on disk under <output_dir>/PRD-<slug>.md.
    slug = forge_intake._slug_for_issue("owner", "repo", 126)
    prd_path = isolated_workdir / "docs" / f"PRD-{slug}.md"
    assert prd_path.exists()


# ── VAL-FORGE-005: plans.prd_file linkage (M3 fix-feature) ───────────────
async def test_intake_persists_prd_file_path_on_plans_row(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """``plans.prd_file`` is set to the absolute path of the generated PRD file.

    Pins VAL-FORGE-005's contract verbatim:
      * ``plan.prd_file`` is non-empty;
      * ``Path(plan.prd_file).is_file() is True``;
      * the file's first line matches ``^# PRD: ``.

    Defence-in-depth: the same path is also mirrored into the
    ``plan.created`` event payload (VAL-CROSS-053).
    """
    import json as _json

    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(305))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/305"))

    rc = await _run_intake(["owner/repo/305"])
    assert rc == forge_intake.EXIT_OK

    slug = forge_intake._slug_for_issue("owner", "repo", 305)
    async with db_pool.acquire() as conn:
        prd_file = await conn.fetchval(
            "SELECT prd_file FROM plans WHERE id = $1",
            slug,
        )

    # Contract: plan.prd_file is non-empty AND points at an existing file.
    assert prd_file is not None and prd_file != ""
    prd_path = Path(prd_file)
    assert prd_path.is_absolute(), f"plans.prd_file must be absolute; got {prd_file!r}"
    assert prd_path.is_file(), f"plans.prd_file must point at an existing file; got {prd_file!r}"

    # Contract: first line matches '^# PRD: '.
    with prd_path.open(encoding="utf-8") as fh:
        first_line = fh.readline()
    assert first_line.startswith("# PRD: "), f"first line of {prd_file!r} must match '^# PRD: '; got {first_line!r}"

    # Defence-in-depth (VAL-CROSS-053 / event payload): the
    # plan.created event payload mirrors the prd_file path.
    async with db_pool.acquire() as conn:
        event_prd_file = await conn.fetchval(
            "SELECT payload->>'prd_file' FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            slug,
        )
    assert event_prd_file == prd_file


# ── VAL-FORGE-005: GET /api/v1/plans/<id> exposes prd_file ──────────────
async def test_api_plans_endpoint_exposes_prd_file(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """``GET /api/v1/plans/<id>`` returns 200 with ``prd_file`` in the body."""
    import json as _json

    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "test-bootstrap-token")
    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(306))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/306"))

    rc = await _run_intake(["owner/repo/306"])
    assert rc == forge_intake.EXIT_OK

    slug = forge_intake._slug_for_issue("owner", "repo", 306)

    app: FastAPI = create_app(
        db_pool,
        worker_token="test-worker-token",
        bootstrap_token="test-bootstrap-token",
        sweep_interval_seconds=60.0,
        offline_worker_sweep_interval_seconds=60.0,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/plans/{slug}")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == slug
            assert isinstance(body["prd_file"], str) and body["prd_file"]
            assert Path(body["prd_file"]).is_file()

            # Pre-existing plans (no Forge intake) carry prd_file=null.
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO plans (id, name) VALUES ($1, $2)",
                    "plain-plan-prd",
                    "Plain Plan",
                )
            plain = await client.get("/api/v1/plans/plain-plan-prd")
            assert plain.status_code == 200
            plain_body = plain.json()
            assert plain_body["prd_file"] is None


# ── VAL-FORGE-012: GET /api/v1/plans/<id> returns github_issue_ref ───────
async def test_api_plans_endpoint_exposes_github_issue_ref(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """``GET /api/v1/plans/<id>`` returns 200 with ``github_issue_ref``.

    Seeds the plan via the intake CLI, then opens a TestClient against
    a fresh ``create_app`` (sharing the test pool) and asserts the
    response shape.
    """
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "test-bootstrap-token")
    import json as _json

    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(127))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/127"))

    rc = await _run_intake(["owner/repo/127"])
    assert rc == forge_intake.EXIT_OK

    # Identify the plan id slug.
    slug = forge_intake._slug_for_issue("owner", "repo", 127)

    app: FastAPI = create_app(
        db_pool,
        worker_token="test-worker-token",
        bootstrap_token="test-bootstrap-token",
        sweep_interval_seconds=60.0,
        offline_worker_sweep_interval_seconds=60.0,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/plans/{slug}")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == slug
            assert body["github_issue_ref"] == "owner/repo/127"

            # VAL-FORGE-012 regression: pre-existing plans (no ref) get NULL.
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO plans (id, name) VALUES ($1, $2)",
                    "plain-plan",
                    "Plain Plan",
                )
            plain = await client.get("/api/v1/plans/plain-plan")
            assert plain.status_code == 200
            assert plain.json()["github_issue_ref"] is None

            # 404 for missing plan id.
            missing = await client.get("/api/v1/plans/does-not-exist")
            assert missing.status_code == 404


# ── VAL-CROSS-001 / VAL-CROSS-053: plan.created event within 200 ms ──────
async def test_intake_emits_plan_created_event_within_200ms(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """Successful intake writes exactly one ``plan.created`` row in ≤ 200 ms.

    Pins both VAL-CROSS-001 (count == 1, latency ≤ 0.2 s) and the
    VAL-CROSS-053 evidence query
    (``payload->>'github_issue_ref' = 'owner/repo/123'``).
    """
    import json as _json
    import time as _time

    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(123))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/123"))

    rc = await _run_intake(["owner/repo/123"])
    # VAL-CROSS-001: the latency budget is "within 200 ms AFTER the CLI
    # exits 0" — i.e. row visibility post-CLI, not total CLI runtime
    # (the CLI also drives the PRD pipeline + tasks generation + two
    # short-lived asyncpg pools, all contract-irrelevant). Measure the
    # window between CLI return and the first successful row read.
    t_after_cli = _time.monotonic()

    assert rc == forge_intake.EXIT_OK

    slug = forge_intake._slug_for_issue("owner", "repo", 123)
    async with db_pool.acquire() as conn:
        # VAL-CROSS-001: exactly one plan.created row for this plan_id.
        count_by_plan = await conn.fetchval(
            "SELECT count(*) FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            slug,
        )
        t_after_query = _time.monotonic()
        assert count_by_plan == 1
        # The same-transaction direct INSERT means the row is committed
        # before the CLI returns; this assertion documents the latency
        # budget rather than gating on the (zero) intra-transaction
        # delay. We allow generous slack for Colima/testcontainers
        # round-trip variance while still pinning VAL-CROSS-001's 200 ms
        # ceiling.
        post_cli_latency = t_after_query - t_after_cli
        assert post_cli_latency < 0.2, (
            f"plan.created row visibility {post_cli_latency * 1000:.1f} ms "
            "after CLI exit — slower than VAL-CROSS-001's 200 ms budget"
        )

        # VAL-CROSS-053: evidence query — exactly one row with the
        # payload-bound issue ref.
        count_by_ref = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='plan.created' AND payload->>'github_issue_ref'=$1",
            "owner/repo/123",
        )
        assert count_by_ref == 1

        # Migration 005 made events.task_id nullable to support the
        # plan-level sentinel; assert the new event respects that
        # convention so an accidental NOT NULL regression would fail.
        task_id = await conn.fetchval(
            "SELECT task_id FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            slug,
        )
        assert task_id is None


# ── Idempotent re-run does NOT double-emit plan.created ──────────────────
async def test_intake_idempotent_re_run_emits_only_one_plan_created(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-running the same intake leaves ``plan.created`` count at exactly 1.

    The Step-1 short-circuit (existing-plan lookup) returns before the
    new INSERT path, so the event emission is naturally skipped on the
    second run — and the loser path under concurrent intake also skips
    it (covered by ``test_intake_concurrent_runs_loser_skips_plan_created_event``).
    """
    import json as _json

    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    gh_recorder.queue(_completed(stdout=_json.dumps(_canned_issue_payload(123))))
    gh_recorder.queue(_completed(stdout="https://github.com/example/repo/issues/123"))

    rc1 = await _run_intake(["owner/repo/123"])
    capsys.readouterr()
    assert rc1 == forge_intake.EXIT_OK

    # Second run — no canned responses queued; the Step-1 short-circuit
    # must fire before any ``gh`` invocation.
    rc2 = await _run_intake(["owner/repo/123"])
    captured = capsys.readouterr()
    assert rc2 == forge_intake.EXIT_OK
    assert "already exists" in captured.out

    slug = forge_intake._slug_for_issue("owner", "repo", 123)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            slug,
        )
    assert count == 1


# ── VAL-FORGE-019 / VAL-CROSS-002: concurrent intake at-most-once edit ───
class _ConcurrentGhRecorder:
    """Thread-safe ``gh_recorder`` for ``asyncio.gather`` of two intake calls.

    The default ``gh_recorder`` fixture pops a per-call canned response
    off a queue, which doesn't fit the concurrent shape (we don't know
    a priori which thread calls ``gh`` first, and the loser may not
    call ``gh issue edit`` at all). This recorder dispatches by argv
    shape:

    * ``["issue", "view", ...]`` → canned issue payload (always).
    * ``["issue", "edit", ...]`` → canned URL stdout (always).

    A ``threading.Lock`` guards the ``calls`` list so concurrent
    ``__call__`` invocations from two intake threads append cleanly.
    """

    def __init__(self, issue_number: int) -> None:
        import threading as _threading

        self._lock = _threading.Lock()
        self._issue_number = issue_number
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        timeout: float = forge_gh.DEFAULT_GH_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if shutil.which("gh") is None:
            raise forge_gh.GHCLIMissingError(
                "gh CLI is not on PATH; install via `brew install gh` or see https://cli.github.com."
            )
        with self._lock:
            self.calls.append(list(args))
        if len(args) >= 2 and args[0] == "issue" and args[1] == "view":
            import json as _json

            return _completed(stdout=_json.dumps(_canned_issue_payload(self._issue_number)))
        if len(args) >= 2 and args[0] == "issue" and args[1] == "edit":
            return _completed(stdout=f"https://github.com/example/repo/issues/{self._issue_number}")
        raise AssertionError(f"_ConcurrentGhRecorder: unexpected gh argv {args!r}")


async def test_intake_concurrent_runs_at_most_one_gh_edit_call(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``asyncio.gather`` of two intakes → 1 plan, ≤ 1 edit, 1 plan.created event.

    Pins VAL-FORGE-019 (exactly one plans row, at most one
    ``gh issue edit`` invocation) and VAL-CROSS-002 (exactly one
    label-mutation call) and VAL-CROSS-001 (exactly one
    ``plan.created`` event). Both invocations exit ``EXIT_OK``; the
    loser's stdout names the same plan id as the winner.
    """
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    recorder = _ConcurrentGhRecorder(issue_number=200)
    monkeypatch.setattr(forge_gh, "_run_gh", recorder)

    # Two concurrent intakes against the same Postgres.
    results = await asyncio.gather(
        _run_intake(["owner/repo/200"]),
        _run_intake(["owner/repo/200"]),
        return_exceptions=False,
    )
    captured = capsys.readouterr()

    # Both invocations exit cleanly — losers print the existing plan id
    # and exit 0 per VAL-FORGE-019's "the losing invocation either
    # exits 0 ... printing the existing plan_id" branch.
    assert results == [forge_intake.EXIT_OK, forge_intake.EXIT_OK], (
        f"both intakes must exit EXIT_OK; got {results!r}; stderr={captured.err}"
    )

    # VAL-FORGE-019: exactly one plans row for the canonical ref.
    slug = forge_intake._slug_for_issue("owner", "repo", 200)
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval(
            "SELECT count(*) FROM plans WHERE github_issue_ref=$1",
            "owner/repo/200",
        )
        assert plan_count == 1

        # VAL-CROSS-001 / VAL-CROSS-053: exactly one plan.created event.
        event_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            slug,
        )
        assert event_count == 1

    # VAL-FORGE-019 / VAL-CROSS-002: at most one ``gh issue edit`` call.
    edit_calls = [c for c in recorder.calls if len(c) >= 2 and c[0:2] == ["issue", "edit"]]
    assert len(edit_calls) <= 1, (
        f"VAL-FORGE-019 / VAL-CROSS-002: at most one `gh issue edit` invocation; got {len(edit_calls)}: {edit_calls!r}"
    )
    # Sanity: at least one ``gh issue view`` was made (the winner's
    # path goes through fetch_issue). The loser may also issue a
    # view if it raced past Step 1; either is allowed.
    view_calls = [c for c in recorder.calls if len(c) >= 2 and c[0:2] == ["issue", "view"]]
    assert len(view_calls) >= 1

    # Loser's stdout includes the same plan_id as the winner's. Both
    # branches (Step-1 short-circuit and Step-5b race-loser) print the
    # plan id followed by "already exists" or "already exists ... nothing
    # to do" — assert the slug appears in stdout.
    assert slug in captured.out


async def test_intake_concurrent_runs_loser_skips_plan_created_event(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    fake_claude: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent intake yields exactly one ``plan.created`` event row.

    Tighter sibling of ``test_intake_concurrent_runs_at_most_one_gh_edit_call``
    focused purely on Part A × Part B interaction: the loser must skip
    BOTH the label flip AND the event emission; the audit log must not
    acquire a duplicate ``plan.created`` row even under the race.
    """
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    recorder = _ConcurrentGhRecorder(issue_number=201)
    monkeypatch.setattr(forge_gh, "_run_gh", recorder)

    results = await asyncio.gather(
        _run_intake(["owner/repo/201"]),
        _run_intake(["owner/repo/201"]),
        return_exceptions=False,
    )
    assert results == [forge_intake.EXIT_OK, forge_intake.EXIT_OK]

    slug = forge_intake._slug_for_issue("owner", "repo", 201)
    async with db_pool.acquire() as conn:
        # Part A × Part B interaction: exactly one plan.created row
        # even under race (loser skipped emission).
        event_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE plan_id=$1 AND event_type='plan.created'",
            slug,
        )
        assert event_count == 1

        # Defence in depth: across the entire events table for this
        # canonical ref (in case a future regression silently skipped
        # the plan_id binding and dropped the row under a different
        # plan_id), the count is still 1.
        count_by_ref = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='plan.created' AND payload->>'github_issue_ref'=$1",
            "owner/repo/201",
        )
        assert count_by_ref == 1
