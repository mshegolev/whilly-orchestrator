"""Whilly v4.0 CLI dispatcher (PRD FR-2.5, FR-3.1, TASK-010b).

This package shadows the legacy v3 :mod:`whilly.cli` module — the actual v3
implementation now lives in :mod:`whilly.cli_legacy`. We re-export every
public symbol of the legacy module so existing code paths keep working
unchanged during the big-bang v4 rewrite (PRD TC-9):

* ``from whilly.cli import main`` — :func:`main` below dispatches v4
  subcommands first, then falls back to the legacy entry point.
* ``from whilly.cli import _log_event`` and other underscore-prefixed
  helpers — re-exported explicitly because :pep:`8` ``from foo import *``
  skips ``_`` names.
* ``patch("whilly.cli.time.sleep")`` and similar attribute-level patches
  used by the v3 test suite — wildcard re-exports the ``time`` module
  reference that legacy ``cli_legacy.py`` imports at the top, so these
  patches keep targeting the same in-memory module object.

Why a dispatcher rather than two separate console scripts?
----------------------------------------------------------
``[project.scripts] whilly = "whilly.cli:main"`` in :file:`pyproject.toml`
points at *this* :func:`main`, not the legacy one. The dispatcher lets us
add v4 subcommands (``whilly plan import``, ``whilly plan show`` from
TASK-015, ``whilly run`` from TASK-019c) without rewriting the v3 argument
parser yet — first token is the routing key, everything else is delegated
to either the v4 sub-CLI or the legacy parser. Once the v4 surface fully
covers the v3 use cases (TASK-029 / TASK-033 territory), the legacy
fallback can be deleted in a single follow-up commit.

PEP 8 complaints about ``import *``
-----------------------------------
We tolerate them here on purpose: the alternative is enumerating ~50
names from the legacy module and keeping that list in sync with every
v3 patch. The test suite already relies on the entire surface being
importable as ``whilly.cli.<X>``, so a wildcard plus an explicit
underscore-prefixed re-export list is the lowest-friction option.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

# Re-export the legacy module surface so existing v3 imports
# (``from whilly.cli import main``, ``patch.object(whilly.cli, "Reporter")``,
# ``patch("whilly.cli.time.sleep")``) keep resolving to the same objects.
# Wildcard misses ``_``-prefixed names, hence the explicit second import.
from whilly.cli_legacy import *  # noqa: F401,F403
from whilly.cli_legacy import (  # noqa: F401
    _emit_json,
    _finalise_project_board,
    _log_event,
)
from whilly.cli_legacy import main as _legacy_main


def main(argv: list[str] | None = None) -> int:
    """v4 CLI entry point — dispatch ``plan`` to v4, everything else to v3.

    The first positional token decides the route:

    * ``whilly plan ...`` → :mod:`whilly.cli.plan` (TASK-010b et al.)
    * anything else (``whilly``, ``whilly --resume``, ``whilly tasks.json``)
      → legacy :func:`whilly.cli_legacy.main`.

    ``argv`` defaults to ``sys.argv[1:]`` to match the legacy contract; we
    pass the *original* ``argv`` (not the post-router slice) to the legacy
    main so its own argument parsing sees the full list it expects.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] == "plan":
        # Lazy import: ``whilly.cli.plan`` pulls in asyncpg via the db adapter.
        # Importing it eagerly at module load time would force every legacy
        # ``whilly`` invocation (including ``whilly --help``) to pay that cost.
        from whilly.cli.plan import run_plan_command

        return run_plan_command(args[1:])
    if args and args[0] == "run":
        # Same lazy-import argument as ``plan`` above: ``whilly.cli.run`` pulls
        # asyncpg + the worker stack; legacy ``whilly --resume`` invocations
        # shouldn't pay that import cost. Owner: TASK-019c.
        from whilly.cli.run import run_run_command

        return run_run_command(args[1:])
    if args and args[0] == "dashboard":
        # Same lazy-import argument as the other v4 subcommands: the Rich
        # Live runtime and asyncpg pool are only paid for by callers that
        # actually want the dashboard. Owner: TASK-027.
        from whilly.cli.dashboard import run_dashboard_command

        return run_dashboard_command(args[1:])
    return _legacy_main(argv)


def run_plan_command(argv: Sequence[str]) -> int:
    """Re-export of :func:`whilly.cli.plan.run_plan_command` for convenience.

    Lets tests that don't need to round-trip through ``main`` invoke the
    plan-subcommand parser directly without importing :mod:`whilly.cli.plan`
    themselves. Implemented as a thin wrapper rather than a re-export at
    import time so we keep the ``asyncpg`` import lazy.
    """
    from whilly.cli.plan import run_plan_command as _run

    return _run(argv)
