"""Whilly v4 CLI dispatcher.

Single entry point for the ``whilly`` console script declared in
:file:`pyproject.toml` (``[project.scripts] whilly = "whilly.cli:main"``).
Routes the first positional token to the matching v4 sub-CLI:

* ``whilly plan ...``      → :mod:`whilly.cli.plan`
* ``whilly run ...``       → :mod:`whilly.cli.run`
* ``whilly dashboard ...`` → :mod:`whilly.cli.dashboard`
* ``whilly init ...``      → :mod:`whilly.cli.init`
* ``whilly worker ...``    → :mod:`whilly.cli.worker`

Every sub-CLI is imported lazily so that ``whilly --help`` (and any other
non-database invocation) does not pull in :mod:`asyncpg`, the dashboard's
Rich Live runtime, or the Claude/agent stack just to print usage text.

Unknown subcommands print the v4 help block to stderr and exit ``2``
(argparse convention). The v3 top-level flags ``--all``, ``--resume``,
``--prd-wizard``, ``--workspace``, and ``--worktree`` are no longer
recognised; their functionality either moved to a v4 subcommand or was
removed (``WHILLY_WORKTREE`` / ``WHILLY_USE_WORKSPACE`` env vars are now
no-ops — the v4 CLI does not act on them).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

__all__ = ["main", "run_plan_command"]


_HELP_TEXT = """\
Whilly v4 — distributed task orchestrator.

Usage: whilly <command> [options]

Commands:
  plan        Manage plans (import, export, show, reset, apply).
  run         Run a local worker that claims tasks from a plan.
  dashboard   Live TUI dashboard for an in-flight plan.
  init        Interactive PRD wizard → plan import.
  worker      Run a remote worker against a control-plane URL.

Run `whilly <command> --help` for command-specific options.
"""


def _print_help(stream: object = None) -> None:
    """Print the v4 help block. Defaults to stdout."""
    out = stream if stream is not None else sys.stdout
    out.write(_HELP_TEXT)
    out.flush()


def main(argv: list[str] | None = None) -> int:
    """v4 CLI entry point — dispatch the first positional token to its sub-CLI.

    ``argv`` defaults to ``sys.argv[1:]`` (matching the standard Python CLI
    contract). When the first token is unknown — including the legacy v3
    flags ``--all``, ``--resume``, ``--prd-wizard``, ``--workspace``, and
    ``--worktree`` — the command prints the v4 help block to stderr and
    returns ``2``.
    """
    args = sys.argv[1:] if argv is None else list(argv)

    if not args or args[0] in ("-h", "--help"):
        _print_help()
        return 0

    if args[0] in ("-V", "--version"):
        from whilly import __version__

        sys.stdout.write(f"whilly {__version__}\n")
        sys.stdout.flush()
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "plan":
        from whilly.cli.plan import run_plan_command

        return run_plan_command(rest)
    if cmd == "run":
        from whilly.cli.run import run_run_command

        return run_run_command(rest)
    if cmd == "dashboard":
        from whilly.cli.dashboard import run_dashboard_command

        return run_dashboard_command(rest)
    if cmd == "init":
        from whilly.cli.init import run_init_command

        return run_init_command(rest)
    if cmd == "worker":
        from whilly.cli.worker import run_worker_command

        return run_worker_command(rest)

    sys.stderr.write(f"whilly: unknown command {cmd!r}\n\n")
    _print_help(sys.stderr)
    return 2


def run_plan_command(argv: Sequence[str]) -> int:
    """Re-export of :func:`whilly.cli.plan.run_plan_command` for convenience.

    Lets tests that don't need to round-trip through :func:`main` invoke the
    plan-subcommand parser directly without importing :mod:`whilly.cli.plan`
    themselves. Implemented as a thin wrapper rather than a re-export at
    import time so we keep the ``asyncpg`` import lazy.
    """
    from whilly.cli.plan import run_plan_command as _run

    return _run(argv)
