"""``whilly init`` — interactive PRD wizard + plan import (TASK-104a-3 / 4).

Composition root that reunites three pieces of the v3 PRD wizard with
the v4 Postgres-backed plan storage:

1. **Slug derivation** — explicit ``--slug`` or auto from the idea text.
2. **PRD generation** — interactive ``prd_launcher`` in a TTY,
   single-shot ``prd_generator.generate_prd`` outside it. Writes
   ``docs/PRD-<slug>.md`` either way.
3. **Plan import** — ``prd_generator.generate_tasks_dict`` builds the
   payload in-memory, ``plan_io.parse_plan_dict`` validates it, and
   ``cli.plan._insert_plan_and_tasks`` does the batched INSERT — same
   helper that ``whilly plan import`` uses, so the v4 import surface
   stays single-sourced.

The scope is intentionally bare: PRD wizard mechanics live in their
existing modules, plan-shape validation lives in the adapter, DB
writes live in the plan CLI's helper. This file is *only* the glue
that selects which path to take and prints the right next-step message.

PRD: ``docs/PRD-v41-prd-wizard-port.md`` (FR-1..FR-8). Backlog tracker:
``.planning/v4-1_tasks.json`` → TASK-104a-3 + TASK-104a-4.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("whilly.cli.init")

# Exit codes (kept identical to the v4 plan/run subcommands so a shell
# script can branch on them uniformly across the CLI surface).
EXIT_OK: int = 0
EXIT_USER_ERROR: int = 1
EXIT_ENVIRONMENT_ERROR: int = 2
EXIT_INTERRUPTED: int = 130

# Env vars (mirror the surface of cli/run.py / cli/plan.py).
DATABASE_URL_ENV: str = "WHILLY_DATABASE_URL"

# Slug derivation: 8 significant words, kebab-case, [a-z0-9-]+ only.
# Kept narrow on purpose — the slug becomes both the PRD filename and
# the plan_id in Postgres, so ASCII-only is the safe choice.
_SLUG_MAX_WORDS: int = 8
_SLUG_VALID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def _slugify(text: str) -> str:
    """Derive a kebab-case slug from arbitrary user input.

    Steps:
      1. Lowercase, strip leading/trailing whitespace.
      2. Replace any non-alphanumeric run with a single hyphen.
      3. Trim to the first :data:`_SLUG_MAX_WORDS` hyphen-separated tokens.
      4. Strip leading/trailing hyphens (no ``-foo`` / ``foo-`` results).
      5. If the result is empty, fall back to ``"plan"`` so the caller
         always gets a valid slug rather than an exception — matching
         the v3 ``generate_prd`` slug-derivation contract that never
         raised.

    The function does not validate the result against
    :data:`_SLUG_VALID_RE` — that's the caller's job (allows ``--slug
    one-letter-x`` to pass).
    """
    lowered = text.lower().strip()
    # Replace runs of non-alphanumeric with one hyphen.
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    cleaned = cleaned.strip("-")
    if not cleaned:
        return "plan"
    tokens = cleaned.split("-")[:_SLUG_MAX_WORDS]
    return "-".join(tokens)


def _validate_slug(slug: str) -> str | None:
    """Return ``None`` if ``slug`` is a legal kebab-case identifier, else error.

    Rules (PRD FR-3): ASCII alphanumeric + hyphens, must start and end
    with an alphanumeric, length ≥ 1. Reject ``--``, ``-foo``, ``foo-``.
    Single-character all-alnum slugs are allowed (operator's risk).
    """
    if not slug:
        return "slug must be non-empty"
    if len(slug) == 1 and slug.isalnum():
        return None
    if not _SLUG_VALID_RE.fullmatch(slug):
        return f"slug {slug!r} must match {_SLUG_VALID_RE.pattern}"
    return None


def _resolve_mode(force_interactive: bool, force_headless: bool) -> str:
    """Pick between ``"interactive"`` and ``"headless"`` PRD-generation flow.

    PRD FR-2: TTY detection by default; ``--interactive`` / ``--headless``
    override unconditionally. Mutual-exclusive guard handled at argparse
    layer; this function trusts the caller.
    """
    if force_interactive:
        return "interactive"
    if force_headless:
        return "headless"
    # Default: interactive only if stdin is a real terminal. Outside a
    # TTY (CI, cron, ssh -T) the interactive Claude session would block
    # on input that never comes — headless single-shot is the safe
    # default there.
    return "interactive" if sys.stdin.isatty() else "headless"


def _build_parser() -> argparse.ArgumentParser:
    """Argparse layout for ``whilly init``. Pulled out for testing."""
    parser = argparse.ArgumentParser(
        prog="whilly init",
        description=(
            "Interactive PRD wizard + plan import. Takes a free-form idea, "
            "produces docs/PRD-<slug>.md via Claude, generates a task plan, "
            "and imports the plan into Postgres. Single-shot replacement "
            "for the v3 'whilly --init' flow."
        ),
    )
    parser.add_argument(
        "idea",
        nargs="+",
        help=(
            "Free-form description of what you want done. Pass it in quotes "
            "or as multiple positional args; they are joined with a space."
        ),
    )
    parser.add_argument(
        "--slug",
        help=(
            "Explicit slug for the PRD filename and plan_id in Postgres. "
            "Default: auto-derive from the first 8 words of <idea>. Must be "
            "kebab-case ([a-z0-9-]+, start/end with alphanumeric)."
        ),
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive Claude session in the current terminal.",
    )
    mode_group.add_argument(
        "--headless",
        action="store_true",
        help="Force single-shot Claude call without interactive questions.",
    )
    parser.add_argument(
        "--no-import",
        action="store_true",
        help=(
            "Save PRD only; do not generate a task plan or import to Postgres. "
            "Useful for inspecting the wizard output before committing to a "
            "plan."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing docs/PRD-<slug>.md. Without --force, an "
            "existing PRD file aborts the run with a hint to pick a "
            "different slug."
        ),
    )
    parser.add_argument(
        "--model",
        default="claude-opus-4-6[1m]",
        help="Claude model name passed through to the wizard / generator.",
    )
    parser.add_argument(
        "--output-dir",
        default="docs",
        help='Directory for the generated PRD file. Default: "docs".',
    )
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--claude-proxy",
        default=None,
        help=(
            "HTTPS proxy URL for Claude only (e.g. http://127.0.0.1:11112). "
            "Overrides WHILLY_CLAUDE_PROXY_URL env var. Whilly's own asyncpg / "
            "httpx connections stay direct via NO_PROXY."
        ),
    )
    proxy_group.add_argument(
        "--no-claude-proxy",
        action="store_true",
        help=("Force-disable Claude proxy even if WHILLY_CLAUDE_PROXY_URL or HTTPS_PROXY is set in the environment."),
    )
    return parser


def _print_next_steps(slug: str, prd_path: Path, task_count: int, no_import: bool) -> None:
    """Standard success message — FR-5 of the PRD."""
    print(f"✓ PRD saved at {prd_path}")
    if no_import:
        print("(--no-import was set; plan was not imported to Postgres)")
        return
    print(f"✓ Plan {slug!r} imported ({task_count} tasks)")
    print("")
    print("Next steps:")
    print(f"  whilly plan show {slug}")
    print(f"  whilly run --plan {slug}")


def run_init_command(
    argv: Sequence[str],
    *,
    interactive_runner=None,
    headless_runner=None,
    tasks_builder=None,
    plan_inserter=None,
) -> int:
    """Execute ``whilly init`` with the given argv.

    Args:
        argv: Argument list (no leading "init" subcommand token).
        interactive_runner / headless_runner / tasks_builder /
        plan_inserter: Test-only seams. ``None`` (default) wires the
            production functions; tests inject fakes to skip the real
            Claude subprocess and Postgres pool.

    Returns:
        Exit code per the module-level constants.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        # argparse exits non-zero on bad input; propagate as user error.
        return EXIT_USER_ERROR if exc.code not in (0, None) else EXIT_OK

    idea = " ".join(args.idea).strip()
    if not idea:
        print("whilly init: idea text cannot be empty", file=sys.stderr)
        return EXIT_USER_ERROR

    if args.slug is not None:
        slug_err = _validate_slug(args.slug)
        if slug_err is not None:
            print(f"whilly init: {slug_err}", file=sys.stderr)
            return EXIT_USER_ERROR
        slug = args.slug
    else:
        slug = _slugify(idea)

    output_dir = Path(args.output_dir)
    prd_path = (output_dir / f"PRD-{slug}.md").resolve()

    # FR-7: idempotency — refuse to overwrite an existing PRD without --force.
    if prd_path.exists() and not args.force:
        print(
            f"whilly init: {prd_path} already exists; pass --force to overwrite or pick another --slug.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    mode = _resolve_mode(args.interactive, args.headless)
    logger.info("whilly init: idea=%r slug=%r mode=%s", idea[:60], slug, mode)

    # ── Step 0: resolve + (optionally) probe Claude proxy ──────────────
    # TASK-109-4: if the operator has a proxy configured (CLI flag, env
    # var, or inherited HTTPS_PROXY), probe the TCP endpoint *once*
    # before we burn time on a real Claude call. The probe surfaces
    # "tunnel not up" as a clear actionable message instead of a
    # confusing connection-refused buried inside Claude's HTTP client.
    # WHILLY_CLAUDE_PROXY_PROBE=0 opts out (e.g. weird proxies that
    # reject TCP probes; we trust the operator).
    from whilly.adapters.runner import proxy as _proxy

    _settings = _proxy.resolve_proxy_settings(
        cli_url=args.claude_proxy,
        cli_disabled=args.no_claude_proxy,
    )
    if _settings.is_active and os.environ.get(_proxy.WHILLY_PROBE_ENV, "1") != "0":
        try:
            assert _settings.url is not None
            _proxy.probe_proxy_or_raise(_settings.url)
        except RuntimeError as probe_exc:
            print(str(probe_exc), file=sys.stderr)
            return EXIT_ENVIRONMENT_ERROR

    # ── Step 1: produce PRD file ────────────────────────────────────────
    try:
        if mode == "interactive":
            runner = interactive_runner or _default_interactive_runner
            prd_exit = runner(idea=idea, slug=slug, output_dir=output_dir, model=args.model)
            if prd_exit != 0 or not prd_path.exists():
                print(
                    "whilly init: wizard exited without saving PRD; rerun or check $CLAUDE_BIN.",
                    file=sys.stderr,
                )
                return EXIT_USER_ERROR
        else:
            runner = headless_runner or _default_headless_runner
            try:
                runner(idea=idea, slug=slug, output_dir=output_dir, model=args.model)
            except RuntimeError as exc:
                print(f"whilly init: PRD generation failed: {exc}", file=sys.stderr)
                return EXIT_USER_ERROR
            if not prd_path.exists():
                print(
                    f"whilly init: generator finished without writing {prd_path}",
                    file=sys.stderr,
                )
                return EXIT_USER_ERROR
    except KeyboardInterrupt:
        print("\nwhilly init: interrupted by user", file=sys.stderr)
        return EXIT_INTERRUPTED

    # ── Step 2: --no-import shortcut ───────────────────────────────────
    if args.no_import:
        _print_next_steps(slug, prd_path, task_count=0, no_import=True)
        return EXIT_OK

    # ── Step 3: build task payload + DB import ─────────────────────────
    builder = tasks_builder or _default_tasks_builder
    inserter = plan_inserter or _default_plan_inserter

    try:
        payload = builder(prd_path=prd_path, plan_id=slug, model=args.model)
    except RuntimeError as exc:
        print(
            f"whilly init: task generation failed: {exc}\n"
            f"PRD left at {prd_path} for inspection; rerun with --force after fixing.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly init: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).\n"
            f"PRD left at {prd_path}; rerun after exporting {DATABASE_URL_ENV}.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    try:
        task_count = inserter(payload=payload, plan_id=slug, dsn=dsn)
    except Exception as exc:  # noqa: BLE001 — diagnostic, not control flow
        print(
            f"whilly init: plan import failed: {exc}\nPRD left at {prd_path}; check Postgres state and rerun.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    _print_next_steps(slug, prd_path, task_count=task_count, no_import=False)
    return EXIT_OK


# ─── Production seams: actual Claude / asyncpg wires ─────────────────────
#
# Pulled out as module-level functions so unit tests can pass cheap fakes
# via the run_init_command keyword arguments without monkeypatching.


def _default_interactive_runner(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
    """Drive ``prd_launcher.run_prd_wizard`` for the interactive TTY flow.

    Wraps the v3 launcher: passes the explicit slug (so the PRD filename
    matches our plan_id), turns off the v3 ``generate_tasks_after``
    bridge (we own task generation in v4 via generate_tasks_dict), and
    forwards the model. Returns the launcher's exit code.

    The ``idea`` text isn't passed through directly — the launcher
    drives an interactive Claude session and the user types the idea
    into Claude themselves. We keep ``idea`` in the signature so this
    mirrors the headless runner and tests can pin the surface.
    """
    from whilly.prd_launcher import run_prd_wizard

    # Interactive launcher prints its own "type your idea" prompt; we
    # just hand it the slug + model and let it block until the user
    # exits Claude.
    del idea  # interactive flow ignores it; user types directly into Claude
    return run_prd_wizard(
        slug=slug,
        output_dir=output_dir,
        generate_tasks_after=False,
        model=model,
    )


def _default_headless_runner(*, idea: str, slug: str, output_dir: Path, model: str) -> None:
    """Drive ``prd_generator.generate_prd`` for the single-shot flow.

    No interactive questions: Claude gets the idea text in one prompt
    and produces the PRD-Markdown directly. The explicit slug param
    (TASK-104a-3 extension to generate_prd) means the file lands at
    docs/PRD-<slug>.md regardless of how the description tokenises.
    """
    from whilly.prd_generator import generate_prd

    generate_prd(description=idea, output_dir=str(output_dir), model=model, slug=slug)


def _default_tasks_builder(*, prd_path: Path, plan_id: str, model: str) -> dict:
    """Drive ``prd_generator.generate_tasks_dict``.

    Indirection layer between the CLI and the generator so tests can
    inject canned payloads without touching the file/Claude pipeline.
    """
    from whilly.prd_generator import generate_tasks_dict

    return generate_tasks_dict(prd_path=prd_path, plan_id=plan_id, model=model)


def _default_plan_inserter(*, payload: dict, plan_id: str, dsn: str) -> int:
    """Validate payload + open pool + INSERT plan and tasks.

    Reuses the existing helpers from cli.plan: parse_plan_dict for
    shape-validation, _async_import for transactional INSERT. Returns
    the count of inserted tasks for the success-message line.
    """
    from whilly.adapters.filesystem.plan_io import parse_plan_dict
    from whilly.cli.plan import _async_import

    plan, tasks = parse_plan_dict(payload, plan_id=plan_id)
    asyncio.run(_async_import(dsn, plan, tasks))
    return len(tasks)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script style entry — splits argv off ``sys.argv``."""
    args = sys.argv[1:] if argv is None else list(argv)
    return run_init_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
