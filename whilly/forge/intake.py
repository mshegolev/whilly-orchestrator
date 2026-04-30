"""``whilly forge intake`` — GitHub Issue → Whilly plan in Postgres (TASK-108a).

Stage 1 of the Forge pipeline. Takes a canonical ``owner/repo/<number>``
triple, fetches the GitHub Issue via ``gh issue view``, normalises the
title + body + comments into a free-form description, runs it through
the existing PRD pipeline (``prd_generator.generate_prd`` +
``generate_tasks_dict``), inserts the resulting plan into Postgres
with ``plans.github_issue_ref = <triple>``, and flips the issue label
from ``whilly-pending`` to ``whilly-in-progress``.

Idempotency
-----------
A partial UNIQUE index on ``plans.github_issue_ref`` (migration 006)
plus an early ``SELECT id FROM plans WHERE github_issue_ref = $1``
ensures re-running ``whilly forge intake owner/repo/123`` against the
same DB:

* Returns the existing ``plan_id`` on stdout and exits 0.
* Does **not** call ``gh issue edit`` a second time (VAL-FORGE-007).
* Does **not** invoke the PRD pipeline (no Claude tokens burned).

Concurrent runs (VAL-FORGE-019) are pinned by the partial UNIQUE: the
loser hits :class:`asyncpg.UniqueViolationError` on INSERT and the
intake pipeline reads back the existing row, exiting 0 with the same
``plan_id`` as the winner.

Atomicity across systems
------------------------
The label flip happens *after* the DB insert succeeds (VAL-FORGE-018):
if any earlier step raises, no plan row is written and no label
transition is performed. The reverse — label flipped but no plan
inserted — is impossible because the label call is the last step.

Error paths
-----------
* ``gh`` not on PATH → exit 2 (``EXIT_ENVIRONMENT_ERROR``) with a
  hint to install (VAL-FORGE-008).
* Issue not found → exit 1 (``EXIT_USER_ERROR``) with a friendly
  "issue not found" message (VAL-FORGE-009).
* Network / transport error → exit 1 with the underlying ``gh``
  stderr (VAL-FORGE-010).
* Malformed ``owner/repo/<N>`` argument → exit 1 *without* invoking
  ``gh`` at all (VAL-FORGE-017).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import asyncpg

from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.filesystem.plan_io import PlanParseError, parse_plan_dict
from whilly.cli.plan import _insert_plan_and_tasks
from whilly.forge._gh import (
    GHCLIError,
    GHCLIMissingError,
    GHIssueNotFoundError,
    fetch_issue,
    flip_label,
)

logger = logging.getLogger(__name__)


# ── Exit codes ───────────────────────────────────────────────────────────
# Mirror the cli/run.py and cli/init.py constants so a shell script can
# branch on them uniformly across the CLI surface.
EXIT_OK: Final[int] = 0
EXIT_USER_ERROR: Final[int] = 1
EXIT_ENVIRONMENT_ERROR: Final[int] = 2
EXIT_INTERRUPTED: Final[int] = 130


# ── Env vars ─────────────────────────────────────────────────────────────
DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"


# ── Label literals ───────────────────────────────────────────────────────
#: GitHub label that an operator applies to an issue to mark it as
#: ready for Forge intake. Forge expects this label as the *source*
#: state of the transition.
LABEL_PENDING: Final[str] = "whilly-pending"

#: GitHub label that Forge applies after a successful intake. Stays
#: on the issue until TASK-108b's Stage 2 flips it again
#: (``whilly-in-review``) on PR open.
LABEL_IN_PROGRESS: Final[str] = "whilly-in-progress"


# ── Issue-ref parsing ────────────────────────────────────────────────────
#: Strict ``owner/repo/<positive integer>`` shape (VAL-FORGE-017). The
#: ``<owner>``/``<repo>`` segments are intentionally lax (any non-slash
#: character) to keep the CLI compatible with GitHub's own naming
#: rules without re-encoding them here; the issue *number* is pinned
#: as ``\d+`` so non-numeric input is rejected early.
_ISSUE_REF_RE: re.Pattern[str] = re.compile(r"^([^/]+)/([^/]+)/(\d+)$")


def _parse_issue_ref(raw: str) -> tuple[str, str, int]:
    """Validate ``owner/repo/<number>`` and return ``(owner, repo, number)``.

    Raises :class:`ValueError` with a message naming the expected shape
    when ``raw`` doesn't match :data:`_ISSUE_REF_RE`. The CLI maps the
    error to ``EXIT_USER_ERROR`` *before* invoking ``gh`` at all
    (VAL-FORGE-017 — zero ``subprocess.run`` calls on malformed input).
    """
    match = _ISSUE_REF_RE.fullmatch(raw)
    if match is None:
        raise ValueError(
            f"issue ref {raw!r} does not match the expected shape "
            f"`owner/repo/<number>` (e.g. mshegolev/whilly-orchestrator/123)"
        )
    owner, repo, number_str = match.group(1), match.group(2), match.group(3)
    if not owner or not repo:
        raise ValueError(f"issue ref {raw!r} has empty owner or repo segment; expected `owner/repo/<number>`")
    return owner, repo, int(number_str)


# ── Issue → idea string (free-form text fed to PRD generator) ────────────
def _issue_to_description(issue: dict[str, Any]) -> str:
    """Synthesise the PRD-generator prompt from a fetched issue payload.

    Concatenates the title, body, and the (chronological) comment
    bodies into a single string. The PRD generator treats the whole
    blob as the operator's free-form idea text — no structural
    parsing is required because the PRD wizard's system prompt is
    the actual prompt; this is just the "describe what you want"
    half.

    Args:
        issue: ``dict`` returned by ``gh issue view ... --json``. Must
            have at least a ``title`` (str) key; ``body`` and
            ``comments`` are optional.
    """
    parts: list[str] = []
    title = issue.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(f"# {title.strip()}")
    body = issue.get("body")
    if isinstance(body, str) and body.strip():
        parts.append(body.strip())
    comments = issue.get("comments") or []
    if isinstance(comments, list) and comments:
        rendered_comments: list[str] = []
        for idx, comment in enumerate(comments, start=1):
            if not isinstance(comment, dict):
                continue
            comment_body = comment.get("body")
            if isinstance(comment_body, str) and comment_body.strip():
                rendered_comments.append(f"### Comment {idx}\n{comment_body.strip()}")
        if rendered_comments:
            parts.append("## Discussion\n\n" + "\n\n".join(rendered_comments))
    return "\n\n".join(parts) or "(empty issue)"


def _slug_for_issue(owner: str, repo: str, number: int) -> str:
    """Deterministic plan slug derived from the issue ref.

    The plan_id is also the PRD filename stem (``docs/PRD-<slug>.md``)
    and the FK target for ``tasks.plan_id``. The chosen shape
    ``issue-<owner>-<repo>-<N>`` is collision-free across owners and
    repos — re-running the same intake hits the same slug, which is
    what ``ON CONFLICT (id) DO NOTHING`` plus the partial UNIQUE on
    ``github_issue_ref`` use to enforce idempotency.

    Slugs are sanitised to ``[a-z0-9-]+`` so they round-trip cleanly
    through filesystem paths and Postgres ``text`` columns.
    """
    raw = f"issue-{owner}-{repo}-{number}".lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return cleaned or f"issue-{number}"


# ── Async DB helpers ─────────────────────────────────────────────────────
_SELECT_PLAN_BY_REF_SQL: Final[str] = """
SELECT id FROM plans WHERE github_issue_ref = $1
"""


_INSERT_PLAN_WITH_GH_REF_SQL: Final[str] = """
INSERT INTO plans (id, name, github_issue_ref)
VALUES ($1, $2, $3)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""


async def _existing_plan_for_ref(conn: asyncpg.Connection, ref: str) -> str | None:
    """Return the ``plans.id`` already bound to ``ref`` (or ``None``)."""
    row = await conn.fetchrow(_SELECT_PLAN_BY_REF_SQL, ref)
    return None if row is None else str(row["id"])


# ── Default seams (production wiring) ────────────────────────────────────
def _default_prd_runner(*, idea: str, slug: str, output_dir: Path, model: str) -> None:
    """Drive ``prd_generator.generate_prd`` for the headless flow.

    Mirrors :func:`whilly.cli.init._default_headless_runner` so tests
    reach for the same surface. Imported lazily so ``whilly forge
    intake --help`` doesn't pull in the prd_generator (which transitively
    pulls in the runner proxy and asyncpg).
    """
    from whilly.prd_generator import generate_prd

    generate_prd(description=idea, output_dir=str(output_dir), model=model, slug=slug)


def _default_tasks_builder(*, prd_path: Path, plan_id: str, model: str) -> dict[str, Any]:
    """Drive ``prd_generator.generate_tasks_dict``."""
    from whilly.prd_generator import generate_tasks_dict

    return generate_tasks_dict(prd_path=prd_path, plan_id=plan_id, model=model)


# ── Argparse ─────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    """Argparse layout for ``whilly forge intake``. Pulled out for testing.

    The help text is deliberately rich (VAL-FORGE-016): operators read
    ``whilly forge intake --help`` to learn the issue-ref shape and the
    label transition contract without grepping the source.
    """
    parser = argparse.ArgumentParser(
        prog="whilly forge intake",
        description=(
            "Forge intake stage — fetch a GitHub Issue, normalise it into "
            "a Whilly plan in Postgres, and flip the issue label "
            "`whilly-pending` -> `whilly-in-progress`. Requires the `gh` "
            "CLI on PATH (`brew install gh` or https://cli.github.com)."
        ),
        epilog=(
            "Issue ref shape: `owner/repo/<number>` (e.g. "
            "mshegolev/whilly-orchestrator/123). The plan row carries the "
            "canonical ref in `plans.github_issue_ref`; re-running with "
            "the same ref returns the existing plan id without creating "
            "a duplicate."
        ),
    )
    parser.add_argument(
        "issue_ref",
        help=("GitHub issue reference in the shape `owner/repo/<number>`. Example: mshegolev/whilly-orchestrator/123."),
    )
    parser.add_argument(
        "--model",
        default="claude-opus-4-6[1m]",
        help="Claude model name passed through to the PRD generator.",
    )
    parser.add_argument(
        "--output-dir",
        default="docs",
        help='Directory for the generated PRD file. Default: "docs".',
    )
    parser.add_argument(
        "--no-label-flip",
        action="store_true",
        help=(
            "Skip the `gh issue edit --remove-label whilly-pending --add-label "
            "whilly-in-progress` step. Useful for dry-runs against issues "
            "that don't carry the `whilly-pending` source label."
        ),
    )
    return parser


# ── Forge subcommand router ──────────────────────────────────────────────
_FORGE_HELP: Final[str] = """\
Whilly Forge — GitHub Issue -> Whilly plan -> PR pipeline.

Usage: whilly forge <subcommand> [options]

Subcommands:
  intake     Fetch a GitHub Issue, normalise into a plan, flip label
             `whilly-pending` -> `whilly-in-progress`. Requires `gh` CLI.

Run `whilly forge intake --help` for full options.
"""


def run_forge_command(argv: Sequence[str]) -> int:
    """Top-level ``whilly forge ...`` dispatcher.

    Currently routes the only registered subcommand (``intake``).
    Future stages (TASK-108b ``compose``) plug in here without
    touching the top-level CLI.
    """
    args = list(argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_FORGE_HELP)
        sys.stdout.flush()
        return EXIT_OK if args else EXIT_OK
    sub = args[0]
    rest = args[1:]
    if sub == "intake":
        return run_forge_intake_command(rest)
    sys.stderr.write(f"whilly forge: unknown subcommand {sub!r}\n\n")
    sys.stderr.write(_FORGE_HELP)
    sys.stderr.flush()
    return EXIT_USER_ERROR


def run_forge_intake_command(
    argv: Sequence[str],
    *,
    fetch_issue_runner=None,
    prd_runner=None,
    tasks_builder=None,
    label_flipper=None,
) -> int:
    """Execute ``whilly forge intake`` with the given argv.

    Args:
        argv: Argument list (no leading "intake" subcommand token).
        fetch_issue_runner: Test seam for :func:`whilly.forge._gh.fetch_issue`.
        prd_runner: Test seam for :func:`_default_prd_runner` (Claude PRD).
        tasks_builder: Test seam for :func:`_default_tasks_builder`
            (Claude tasks JSON).
        label_flipper: Test seam for :func:`whilly.forge._gh.flip_label`.

    Returns:
        Exit code per the module-level constants. ``EXIT_OK`` on
        success, ``EXIT_USER_ERROR`` for input validation / 404 on
        the issue, ``EXIT_ENVIRONMENT_ERROR`` for missing ``gh`` /
        ``WHILLY_DATABASE_URL``.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        # argparse's ``--help`` exits 0; bad args exit 2 inside argparse.
        # Map 2 → user error to match the rest of the CLI.
        if exc.code in (0, None):
            return EXIT_OK
        return EXIT_USER_ERROR

    # ── Step 0: parse the issue ref ────────────────────────────────────
    # VAL-FORGE-017: malformed input MUST be rejected before any
    # ``subprocess.run`` is invoked. ``_parse_issue_ref`` is pure.
    try:
        owner, repo, number = _parse_issue_ref(args.issue_ref)
    except ValueError as exc:
        print(f"whilly forge intake: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    canonical_ref = f"{owner}/{repo}/{number}"
    slug = _slug_for_issue(owner, repo, number)

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly forge intake: {DATABASE_URL_ENV} is not set — point it at a "
            "Postgres instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    # ── Step 1: idempotent short-circuit on existing plan ──────────────
    # If the same ref is already in plans, return the existing id and
    # exit without invoking ``gh`` (VAL-FORGE-007).
    existing_id = asyncio.run(_lookup_existing_plan(dsn, canonical_ref))
    if existing_id is not None:
        print(
            f"whilly forge intake: plan {existing_id!r} already exists for "
            f"github_issue_ref={canonical_ref!r}; nothing to do."
        )
        return EXIT_OK

    # ── Step 2: fetch the issue payload via gh ─────────────────────────
    fetch = fetch_issue_runner or fetch_issue
    try:
        issue = fetch(owner, repo, number)
    except GHCLIMissingError as exc:
        print(f"whilly forge intake: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR
    except GHIssueNotFoundError as exc:
        print(
            f"whilly forge intake: issue not found: {canonical_ref}\n"
            f"  (gh exit={exc.returncode}; stderr={exc.stderr.strip()!r})",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    except GHCLIError as exc:
        print(f"whilly forge intake: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    # ── Step 3: normalise issue → idea text ────────────────────────────
    description = _issue_to_description(issue)

    # ── Step 4: drive PRD pipeline ─────────────────────────────────────
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prd_path = (output_dir / f"PRD-{slug}.md").resolve()
    prd = prd_runner or _default_prd_runner
    builder = tasks_builder or _default_tasks_builder
    try:
        prd(idea=description, slug=slug, output_dir=output_dir, model=args.model)
    except RuntimeError as exc:
        print(
            f"whilly forge intake: PRD generation failed: {exc}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    if not prd_path.exists():
        print(
            f"whilly forge intake: PRD generator did not write {prd_path}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    try:
        payload = builder(prd_path=prd_path, plan_id=slug, model=args.model)
    except RuntimeError as exc:
        print(
            f"whilly forge intake: task generation failed: {exc}\nPRD left at {prd_path} for inspection.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    except FileNotFoundError as exc:
        print(
            f"whilly forge intake: PRD missing: {exc}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    # ── Step 5: validate plan dict + INSERT (with github_issue_ref) ────
    try:
        plan, tasks = parse_plan_dict(payload, plan_id=slug)
    except PlanParseError as exc:
        print(
            f"whilly forge intake: generated plan failed validation: {exc}\nPRD left at {prd_path}.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    try:
        plan_id = asyncio.run(
            _async_intake_insert(
                dsn=dsn,
                plan_id=slug,
                plan_name=plan.name,
                github_issue_ref=canonical_ref,
                tasks=tasks,
            )
        )
    except Exception as exc:  # noqa: BLE001 — diagnostic, not control flow
        print(
            f"whilly forge intake: plan import failed: {exc}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    # ── Step 6: label transition ───────────────────────────────────────
    # Last step on purpose (VAL-FORGE-018): if anything earlier fails,
    # we exit before the label flip and the issue stays untouched.
    if args.no_label_flip:
        logger.info(
            "forge.intake: skipping label flip for %s (--no-label-flip)",
            canonical_ref,
        )
    else:
        flipper = label_flipper or flip_label
        try:
            flipper(
                owner,
                repo,
                number,
                add=LABEL_IN_PROGRESS,
                remove=LABEL_PENDING,
            )
        except GHCLIError as exc:
            # VAL-FORGE-013 / operator-friendliness: if ``gh issue edit``
            # raises after the plan is already in DB, surface the
            # warning but exit 0 — the operator can flip the label
            # manually. The plan row is already correct.
            print(
                f"whilly forge intake: warning: label flip failed for "
                f"{canonical_ref}: {exc}\n"
                f"Plan {plan_id} was created; flip the label manually:\n"
                f"  gh issue edit {number} --repo {owner}/{repo} "
                f"--remove-label {LABEL_PENDING} --add-label {LABEL_IN_PROGRESS}",
                file=sys.stderr,
            )

    print(
        f"whilly forge intake: created plan {plan_id!r} from {canonical_ref!r} ({len(tasks)} task(s)). PRD: {prd_path}"
    )
    return EXIT_OK


# ── Async insert helper ──────────────────────────────────────────────────
async def _lookup_existing_plan(dsn: str, ref: str) -> str | None:
    """Open a short-lived pool, return the plan id bound to ``ref`` (or None).

    Idiomatic short-lived pool lifecycle (matches ``cli.plan._async_*``).
    Returns ``None`` if the ref isn't claimed yet.
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            return await _existing_plan_for_ref(conn, ref)
    finally:
        await close_pool(pool)


async def _async_intake_insert(
    *,
    dsn: str,
    plan_id: str,
    plan_name: str,
    github_issue_ref: str,
    tasks: list,
) -> str:
    """Insert plan (with ``github_issue_ref``) + tasks atomically.

    Mirrors :func:`whilly.cli.plan._async_import` but uses the new
    ``_INSERT_PLAN_WITH_GH_REF_SQL`` for the plan row and reuses the
    existing :func:`_insert_plan_and_tasks` helper for the per-task
    INSERTs. Returns the persisted plan id.

    Idempotency under concurrent runs (VAL-FORGE-019)
    -------------------------------------------------
    The partial UNIQUE on ``plans.github_issue_ref`` (migration 006)
    lets us tolerate the race where two concurrent intake processes
    both pass the existence check (Step 1 above) but only one wins
    the INSERT. The loser catches :class:`asyncpg.UniqueViolationError`
    and reads back the winner's row. Either subprocess exits 0 with
    the same ``plan_id``.
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # First, attempt to insert the plan with github_issue_ref.
                try:
                    inserted_id = await conn.fetchval(
                        _INSERT_PLAN_WITH_GH_REF_SQL,
                        plan_id,
                        plan_name,
                        github_issue_ref,
                    )
                except asyncpg.UniqueViolationError:
                    # Concurrent intake won — the partial UNIQUE on
                    # github_issue_ref refused our duplicate. Read
                    # back the winner's row and return early without
                    # inserting any tasks (the winner's transaction
                    # already inserted them, or will once it commits).
                    inserted_id = None
                if inserted_id is None:
                    existing = await _existing_plan_for_ref(conn, github_issue_ref)
                    if existing is not None:
                        return existing
                    # ON CONFLICT (id) — same plan_id slug but a
                    # different github_issue_ref already claims it.
                    # This is a slug collision (extremely unlikely
                    # given _slug_for_issue's shape); surface as a
                    # plain RuntimeError so the caller sees something
                    # actionable.
                    raise RuntimeError(
                        f"plan id {plan_id!r} already exists with a different "
                        f"github_issue_ref; cannot intake {github_issue_ref!r}."
                    )
                # Insert the tasks alongside the plan in the same
                # transaction (matches ``whilly plan import`` shape).
                await _insert_plan_and_tasks(conn, _PlanProxy(plan_id, plan_name), tasks)
                return inserted_id
    finally:
        await close_pool(pool)


class _PlanProxy:
    """Tiny shim so :func:`_insert_plan_and_tasks` sees a Plan-shaped object.

    ``cli.plan._insert_plan_and_tasks`` runs the plan INSERT itself —
    we ran our own (with the github_issue_ref column) above, so we
    just want it to skip past the plan row and insert tasks. Passing
    a real :class:`whilly.core.models.Plan` would re-INSERT the same
    row (harmless under ``ON CONFLICT (id) DO NOTHING``, but a wasted
    round-trip). We keep this proxy minimal so a future refactor can
    split ``_insert_plan_and_tasks`` into ``_insert_plan`` and
    ``_insert_tasks``.
    """

    __slots__ = ("id", "name")

    def __init__(self, plan_id: str, name: str) -> None:
        self.id = plan_id
        self.name = name


__all__ = [
    "DATABASE_URL_ENV",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USER_ERROR",
    "LABEL_IN_PROGRESS",
    "LABEL_PENDING",
    "run_forge_command",
    "run_forge_intake_command",
]
