"""Whilly v4 CLI dispatcher.

Single entry point for the ``whilly`` console script declared in
:file:`pyproject.toml` (``[project.scripts] whilly = "whilly.cli:main"``).
Routes the first positional token to the matching v4 sub-CLI:

* ``whilly plan ...``      → :mod:`whilly.cli.plan`
* ``whilly run ...``       → :mod:`whilly.cli.run`
* ``whilly dashboard ...`` → :mod:`whilly.cli.dashboard`
* ``whilly init ...``      → :mod:`whilly.cli.init`
* ``whilly worker ...``    → :mod:`whilly.cli.worker`
* ``whilly forge ...``     → :mod:`whilly.forge`

Every sub-CLI is imported lazily so that ``whilly --help`` (and any other
non-database invocation) does not pull in :mod:`asyncpg`, the dashboard's
Rich Live runtime, or the Claude/agent stack just to print usage text.

Legacy v3 top-level flag shim
-----------------------------
The v3 console accepted top-level long flags such as
``whilly --tasks tasks.json``, ``whilly --headless``, ``whilly --init …``,
``whilly --prd-wizard``, ``whilly --resume``, ``whilly --reset PLAN``,
``whilly --all`` and the workspace/worktree opt-in toggles
``--workspace`` / ``--worktree`` / ``--no-workspace`` / ``--no-worktree``.
``CLAUDE.md`` documents these as the canonical user-facing entry points
and explicitly lists ``--no-workspace`` / ``--no-worktree`` as no-ops
"retained for backward compatibility". STRICT backwards compatibility is
mission-critical (see ``AGENTS.md``), so :func:`_apply_legacy_shim`
detects each legacy form *before* the v4 unknown-command rejection path
and rewrites ``argv`` into the equivalent v4 subcommand invocation:

==================================  ==========================================
Legacy invocation                    v4 dispatch
==================================  ==========================================
``whilly --tasks PATH``              ``whilly run --plan PATH``
``whilly --headless``                Sets ``WHILLY_HEADLESS=1``; stripped from argv
``whilly --init "desc"``             ``whilly init "desc"``
``whilly --prd-wizard [SLUG]``       ``whilly init --interactive [--slug SLUG] [SLUG]``
``whilly --resume``                  No-op (Postgres state survives restarts)
``whilly --reset PLAN``              ``whilly plan reset PLAN --keep-tasks --yes``
``whilly --all``                     No-op (use ``whilly run --plan <id>`` per plan)
``--workspace`` / ``--worktree``     Silently consumed (opt-in workspace toggle)
``--no-workspace`` / ``--no-worktree``  Silently consumed (legacy no-ops)
==================================  ==========================================

The legacy ``--init`` flow additionally tolerates the v3-only
``--plan`` / ``--go`` modifier flags by dropping them — the v4 ``init``
subcommand always imports + can be followed by a separate
``whilly run`` step, so the modifiers carry no behavioural meaning in
v4.
"""

from __future__ import annotations

import os
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
              `whilly worker register` mints a per-worker bearer token.
  forge       GitHub Issue → Whilly plan pipeline (`forge intake`).

Run `whilly <command> --help` for command-specific options.

Legacy v3 flag forms (`whilly --tasks PATH`, `whilly --init …`,
`whilly --prd-wizard`, `whilly --resume`, `whilly --reset PLAN`,
`whilly --all`, `--workspace`/`--worktree` opt-ins, and the
`--no-workspace`/`--no-worktree` no-ops) are accepted for backwards
compatibility and routed to the v4 subcommand surface above.
"""


# ────────────────────────────────────────────────────────────────────────
# Legacy v3 flag shim
# ────────────────────────────────────────────────────────────────────────
#
# These constants are the source of truth for which legacy forms still
# need to be accepted post-M1. Adding or removing a token here must be
# accompanied by a matching change in
# :file:`tests/unit/test_cli_legacy_flag_shim.py` so the routing
# contract stays tested.
_LEGACY_NOOP_FLAGS: frozenset[str] = frozenset(
    {
        # Workspace/worktree toggles — opt-in path is preserved by
        # ``WHILLY_USE_WORKSPACE`` env var; the long flags are accepted
        # and silently consumed so legacy scripts that pass them do not
        # die with "unrecognized arguments".
        "--workspace",
        "--worktree",
        "--no-workspace",
        "--no-worktree",
    }
)

_LEGACY_VERB_FLAGS: frozenset[str] = frozenset(
    {
        "--tasks",
        "--headless",
        "--init",
        "--prd-wizard",
        "--resume",
        "--reset",
        "--all",
    }
)


def _print_help(stream: object = None) -> None:
    """Print the v4 help block. Defaults to stdout."""
    out = stream if stream is not None else sys.stdout
    out.write(_HELP_TEXT)
    out.flush()


def _apply_legacy_shim(args: list[str]) -> tuple[list[str] | None, int | None]:
    """Translate v3-era top-level flags into a v4 subcommand invocation.

    Returns a 2-tuple ``(new_args, exit_code)``:

    * ``(None, None)`` — argv does not match any legacy form; caller
      proceeds with the original ``args`` unchanged.
    * ``(list, None)`` — caller should dispatch using the rewritten args.
    * ``(None, int)`` — caller exits immediately with this code (used
      for legacy forms that have no v4 equivalent and only emit a
      diagnostic, e.g. ``whilly --resume`` and ``whilly --all``).

    Side effects:
      * Sets ``WHILLY_HEADLESS=1`` in :data:`os.environ` when the
        legacy ``--headless`` flag appears anywhere in ``args``. The v4
        subcommand then reads it from the environment (matches the v3
        non-TTY contract documented in
        ``VAL-CROSS-BACKCOMPAT-009``).
    """
    if not args:
        return None, None

    # Fast reject: nothing here looks legacy. Tokens that start with
    # ``--`` but aren't in the legacy set fall through to the normal v4
    # dispatcher (which rejects them with a clear "unknown command"
    # error). This keeps the shim narrow.
    has_noop = any(a in _LEGACY_NOOP_FLAGS for a in args)
    has_headless = "--headless" in args
    has_verb = any(a in _LEGACY_VERB_FLAGS for a in args)
    if not (has_noop or has_headless or has_verb):
        return None, None

    # Strip pure no-op tokens (workspace/worktree variants) from anywhere
    # in argv. They never affect downstream parsing in v4.
    rest = [a for a in args if a not in _LEGACY_NOOP_FLAGS]

    # ``--headless`` is a state-setting modifier. v3 uses it to pick the
    # JSON-on-stdout exit-code contract; in v4 the same env var is read
    # by ``whilly init`` (TTY detection) and by ``whilly run`` consumers
    # via :data:`whilly.config`, so we just export it and drop the
    # token. This matches ``CLAUDE.md``'s description of the flag as a
    # mode toggle rather than a verb.
    if "--headless" in rest:
        os.environ["WHILLY_HEADLESS"] = "1"
        rest = [a for a in rest if a != "--headless"]

    # If the only legacy tokens were modifiers (workspace + headless),
    # there is nothing left to dispatch. Fall back to the v4 default
    # behaviour by returning an empty argv — :func:`main` then prints
    # help and exits 0, matching v3's "no plan, just show help" flow.
    if not rest:
        return [], None

    head = rest[0]

    # If the only legacy tokens were no-op modifiers (no legacy verb in
    # head position), return the cleaned args so v4 dispatch sees an
    # invocation like ``whilly run ...`` or ``whilly --help`` without
    # the workspace/worktree noise.
    if head not in _LEGACY_VERB_FLAGS:
        return rest, None

    if head == "--tasks":
        # v3: ``whilly --tasks PATH`` runs the in-process Wiggum loop on
        # the JSON plan at PATH. v4 keeps the same operator mental model
        # by routing to ``whilly run --plan PATH`` — the v4 worker
        # claims tasks from whichever plan id matches PATH (operators
        # who have already imported the plan use the same identifier
        # they imported under).
        if len(rest) < 2 or rest[1].startswith("-"):
            sys.stderr.write("whilly: --tasks requires a path or plan id (e.g. `whilly --tasks tasks.json`).\n")
            return None, 2
        return ["run", "--plan", rest[1], *rest[2:]], None

    if head == "--init":
        # v3 supported ``--plan`` and ``--go`` modifiers on the init
        # pipeline — they're meaningless in v4 (init always imports;
        # follow up with ``whilly run`` to execute) so we strip them.
        cleaned = [a for a in rest[1:] if a not in ("--plan", "--go")]
        if not cleaned:
            sys.stderr.write('whilly: --init requires a description (e.g. `whilly --init "build a thing"`).\n')
            return None, 2
        return ["init", *cleaned], None

    if head == "--prd-wizard":
        # v3: ``whilly --prd-wizard [SLUG]`` launches Claude
        # interactively with the PRD master prompt. v4 fuses this into
        # ``whilly init --interactive`` (FR-2 of PRD-v41-prd-wizard-port).
        # We pass through ``--help`` so the wizard's argparse help
        # prints rather than spawning Claude.
        tail = rest[1:]
        if "--help" in tail or "-h" in tail:
            return ["init", "--help"], None

        slug: str | None = None
        passthrough: list[str] = []
        for token in tail:
            if token.startswith("-"):
                passthrough.append(token)
            elif slug is None:
                slug = token
            else:
                passthrough.append(token)

        new_args: list[str] = ["init", "--interactive"]
        if slug is not None:
            new_args += ["--slug", slug, slug]
        else:
            # v4 ``init`` requires a non-empty idea positional; supply a
            # neutral placeholder so the dispatcher still routes
            # cleanly. The interactive wizard ignores the idea text and
            # the user types directly into Claude (see
            # :func:`whilly.cli.init._default_interactive_runner`).
            new_args += ["wizard"]
        new_args += passthrough
        return new_args, None

    if head == "--resume":
        # v3 reloaded ``.whilly_state.json`` and continued the
        # in-process loop. v4's state is in Postgres, so re-running
        # ``whilly run`` resumes naturally — there's nothing to do here.
        # Keeping this as a no-op (with a one-line stderr breadcrumb)
        # means legacy operator shell wrappers don't break.
        sys.stderr.write(
            "whilly: --resume is a no-op in v4 — Postgres state survives "
            "restarts; rerun `whilly run --plan <plan_id>` to continue.\n"
        )
        return None, 0

    if head == "--reset":
        # v3: ``whilly --reset PLAN`` reset every task in PLAN to
        # PENDING. v4 has the equivalent under
        # ``whilly plan reset --keep-tasks``; we bake in ``--yes`` so
        # legacy non-interactive scripts don't hang on the y/N prompt.
        if len(rest) < 2 or rest[1].startswith("-"):
            sys.stderr.write("whilly: --reset requires a plan id (e.g. `whilly --reset tasks.json`).\n")
            return None, 2
        return ["plan", "reset", rest[1], "--keep-tasks", "--yes"], None

    if head == "--all":
        # v3 ran every discovered ``tasks*.json`` plan sequentially. v4
        # plans live in Postgres and are run individually with
        # ``whilly run --plan <id>``. No automatic "run all plans"
        # exists; emit a diagnostic and exit 0 so legacy scripts don't
        # break.
        sys.stderr.write(
            "whilly: --all is a no-op in v4 — run each plan explicitly with `whilly run --plan <plan_id>`.\n"
        )
        return None, 0

    # Token started with ``--`` but didn't match the legacy table — let
    # the caller fall through to the regular unknown-command path.
    return None, None


def main(argv: list[str] | None = None) -> int:
    """v4 CLI entry point — dispatch the first positional token to its sub-CLI.

    ``argv`` defaults to ``sys.argv[1:]`` (matching the standard Python CLI
    contract). Legacy v3 top-level flags (``--tasks``, ``--headless``,
    ``--init``, ``--prd-wizard``, ``--resume``, ``--reset``, ``--all``,
    ``--workspace``/``--worktree``/``--no-workspace``/``--no-worktree``)
    are intercepted by :func:`_apply_legacy_shim` and rewritten into the
    matching v4 subcommand before the unknown-command rejection runs.
    """
    args = sys.argv[1:] if argv is None else list(argv)

    # ── legacy v3 flag shim ────────────────────────────────────────
    # Run the shim before -h/--help and -V/--version so legacy
    # invocations like ``whilly --headless --help`` route into
    # ``whilly --help`` after the headless modifier is consumed.
    shim_args, shim_exit = _apply_legacy_shim(args)
    if shim_exit is not None:
        return shim_exit
    if shim_args is not None:
        args = shim_args

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
    if cmd == "forge":
        # Lazy import keeps ``whilly --help`` fast — the forge package
        # transitively imports asyncpg + the PRD generator stack.
        from whilly.forge.intake import run_forge_command

        return run_forge_command(rest)
    if cmd == "worker":
        # Sub-dispatch ``whilly worker register ...`` and
        # ``whilly worker connect ...`` to their handlers before falling
        # through to the main loop entry point — mirrors the standalone
        # ``whilly-worker`` console script's behaviour (see
        # :func:`whilly.cli.worker.main`). Keeps a single source of
        # truth for each subcommand's CLI shape regardless of which
        # binary the operator invokes.
        if rest and rest[0] == "register":
            from whilly.cli.worker import run_register_command

            return run_register_command(rest[1:])
        if rest and rest[0] == "connect":
            from whilly.cli.worker import run_connect_command

            return run_connect_command(rest[1:])
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
