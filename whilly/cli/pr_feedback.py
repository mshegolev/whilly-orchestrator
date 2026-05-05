"""``whilly pr-feedback`` subcommand — manual one-shot poll of open PRs.

This is the user-facing entry point that runs a single
:func:`whilly.sources.github_pr_feedback.poll_pr_feedback` cycle
against a given plan id and exits 0 on success. Long-running polling
(every ``WHILLY_PR_FEEDBACK_POLL_INTERVAL`` seconds) is intentionally
out of scope for the M2 mission — operators can wrap this command in a
shell loop or a systemd timer for now.

Subcommand surface
------------------
* ``whilly pr-feedback poll --plan <id>``
  Runs one poll cycle. On success, prints a single-line summary
  mentioning the plan id and the number of PRs polled, then exits 0
  (VAL-PR-019). On missing ``WHILLY_DATABASE_URL``, exits non-zero
  with a stderr diagnostic naming the env var (VAL-PR-020).

Exit codes
----------
* ``0`` — poll cycle completed successfully.
* ``1`` — operation-level failure (asyncpg connection error,
  unexpected exception inside the poller). The CLI prints the
  diagnostic to stderr.
* ``2`` — environment / argparse failure: ``WHILLY_DATABASE_URL``
  unset, required arg missing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from typing import Final

from whilly.adapters.db import TaskRepository, close_pool, create_pool
from whilly.audit import JsonlEventSink
from whilly.sources.github_pr_feedback import poll_pr_feedback

__all__ = [
    "DATABASE_URL_ENV",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "EXIT_OPERATION_ERROR",
    "build_pr_feedback_parser",
    "run_pr_feedback_command",
]

logger = logging.getLogger(__name__)


DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"

EXIT_OK: Final[int] = 0
EXIT_OPERATION_ERROR: Final[int] = 1
EXIT_ENVIRONMENT_ERROR: Final[int] = 2


_TOP_HELP: Final[str] = """\
Whilly PR-feedback — poll open PRs for a plan and emit review events.

Usage: whilly pr-feedback <subcommand> [options]

Subcommands:
  poll        Run one poll cycle against open PRs for --plan <id>.

Run `whilly pr-feedback <subcommand> --help` for full options.
"""


def build_pr_feedback_parser() -> argparse.ArgumentParser:
    """Build the ``whilly pr-feedback poll ...`` argparse tree.

    Pulled into its own factory so tests can introspect the argparse
    surface without invoking the side-effecting handler — same pattern
    as :func:`whilly.cli.run.build_run_parser`.
    """
    parser = argparse.ArgumentParser(
        prog="whilly pr-feedback poll",
        description=(
            "Run one PR-feedback poll cycle for the given plan id. "
            "Reads open `pull_requests` rows from Postgres, runs the "
            "documented `gh pr view` / `gh api .../reviews` / "
            "`gh api .../comments` triple per PR, and emits "
            "`pr.review.changes_requested` / `pr.review.approved` / "
            "`pr.merged` events as appropriate. Long-running polling "
            "(every WHILLY_PR_FEEDBACK_POLL_INTERVAL seconds) is out "
            "of scope; wrap this command in a shell loop or systemd "
            "timer if you need it."
        ),
    )
    parser.add_argument(
        "--plan",
        dest="plan_id",
        required=True,
        help="Plan id whose open PRs to poll.",
    )
    return parser


def run_pr_feedback_command(argv: Sequence[str]) -> int:
    """Top-level ``whilly pr-feedback ...`` dispatcher.

    Currently routes the only registered subcommand (``poll``).
    Future subcommands (e.g. ``status``, ``ack``) plug in here without
    touching the top-level CLI dispatcher in :mod:`whilly.cli`.
    """
    args = list(argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_TOP_HELP)
        sys.stdout.flush()
        return EXIT_OK
    sub = args[0]
    rest = args[1:]
    if sub == "poll":
        return _run_poll_subcommand(rest)
    sys.stderr.write(f"whilly pr-feedback: unknown subcommand {sub!r}\n\n")
    sys.stderr.write(_TOP_HELP)
    sys.stderr.flush()
    return EXIT_OPERATION_ERROR


def _run_poll_subcommand(argv: Sequence[str]) -> int:
    parser = build_pr_feedback_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        if exc.code in (0, None):
            return EXIT_OK
        raise

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly pr-feedback: {DATABASE_URL_ENV} is not set — point it at a "
            "Postgres instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    plan_id: str = args.plan_id

    try:
        polled = asyncio.run(_async_poll_one_cycle(dsn=dsn, plan_id=plan_id))
    except Exception as exc:  # noqa: BLE001 — surface clean diagnostic.
        logger.exception("whilly pr-feedback poll: cycle failed")
        print(
            f"whilly pr-feedback: poll cycle for plan {plan_id!r} failed — {exc}",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR

    print(
        f"whilly pr-feedback: plan={plan_id} polled {polled} open PR(s)",
        file=sys.stdout,
    )
    return EXIT_OK


async def _async_poll_one_cycle(*, dsn: str, plan_id: str) -> int:
    """Open the pool, attach the JSONL mirror, run one poll cycle.

    Pool lifecycle is local to this call. The ``finally`` always closes
    the pool so a SIGTERM caught mid-cycle still drains connections to
    Postgres. Returns the number of PRs polled successfully (matches the
    return contract of :func:`poll_pr_feedback`).
    """
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool, jsonl_sink=JsonlEventSink())
        return await poll_pr_feedback(repo, plan_id)
    finally:
        await close_pool(pool)
