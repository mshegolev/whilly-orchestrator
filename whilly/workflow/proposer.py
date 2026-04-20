"""Interactive mapping proposer.

Reads a :class:`~whilly.workflow.base.GapReport`, walks the user through
gaps, and emits a finalised :class:`~whilly.workflow.base.WorkflowMapping`
that can be persisted via :func:`whilly.workflow.analyzer.save_mapping`.

Three run modes cover the practical permutations:

* **interactive** (default when stdin is a TTY) — prompt per gap, accept
  add/map/skip choices, persist the chosen mapping on success.
* **apply** — non-interactive "add all missing columns" path; suitable for
  a human running it locally before committing the mapping file. Skips
  boards where ``add_status`` isn't implemented (tolerates the Jira-classic
  case cleanly).
* **report** — dry-run: no prompts, no writes, return a proposal object
  describing what *would* happen.

The actual board mutation (``add_status``) happens through the Protocol —
adapter authors decide what's possible.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Callable

from whilly.workflow.base import BoardSink, BoardStatus, GapReport, WorkflowMapping

log = logging.getLogger("whilly.workflow.proposer")


# ── Proposal data ────────────────────────────────────────────────────────────


@dataclass
class Proposal:
    """What the proposer decided to do for each gap.

    Kept separate from :class:`WorkflowMapping` so callers can inspect the
    plan (dry-run mode) without committing.
    """

    to_add: list[str] = field(default_factory=list)  # event names whose status we'll create
    to_map: dict[str, BoardStatus] = field(default_factory=dict)  # event → existing status
    to_skip: list[str] = field(default_factory=list)  # event names deliberately unmapped
    cancelled: bool = False


# ── Prompt primitives ────────────────────────────────────────────────────────


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str, reader: Callable[[str], str] | None = None) -> str:
    """Read a line with an optional injected reader (tests pass a fake).

    Returns the stripped input. EOF → empty string (treated as "skip" by
    the caller — matches the non-interactive fallback).
    """
    try:
        return (reader or input)(prompt).strip()
    except EOFError:
        return ""


# ── Interactive resolution ───────────────────────────────────────────────────


def _choose_from_list(options: list[BoardStatus], reader: Callable[[str], str] | None = None) -> BoardStatus | None:
    """Show a numbered menu of statuses, return the one the user picked
    (or ``None`` for skip).
    """
    for i, st in enumerate(options, 1):
        print(f"    {i}. {st.name}")
    print("    0. skip")
    choice = _ask("    pick #: ", reader)
    if not choice or choice == "0":
        return None
    try:
        idx = int(choice)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    except ValueError:
        pass
    print("    invalid choice — skipped")
    return None


def _resolve_missing(
    event: str,
    statuses: list[BoardStatus],
    reader: Callable[[str], str] | None = None,
) -> tuple[str, BoardStatus | None]:
    """Ask what to do about a missing event. Returns (action, status_or_None):

    * ("add",  None) — user wants a new status column
    * ("map",  status) — map to an existing status
    * ("skip", None) — leave unmapped
    """
    print(f"\n  ✗ '{event}' — no matching board status.")
    print("    [A] add a new column on the board")
    print("    [M] map to an existing status")
    print("    [S] skip (whilly won't move cards for this event)")
    choice = _ask("    choice [A/M/S]: ", reader).lower()
    if choice.startswith("a"):
        return "add", None
    if choice.startswith("m"):
        print("    existing statuses:")
        st = _choose_from_list(statuses, reader)
        return ("map", st) if st else ("skip", None)
    return "skip", None


def _resolve_ambiguous(
    event: str,
    candidates: list[BoardStatus],
    reader: Callable[[str], str] | None = None,
) -> BoardStatus | None:
    """Pick one of the ambiguous candidates, or ``None`` to skip."""
    print(f"\n  ⚠ '{event}' — multiple matches, pick one:")
    return _choose_from_list(candidates, reader)


# ── Orchestrator ─────────────────────────────────────────────────────────────


def propose(
    report: GapReport,
    board: BoardSink,
    existing: WorkflowMapping | None = None,
    *,
    mode: str = "auto",
    reader: Callable[[str], str] | None = None,
) -> tuple[Proposal, WorkflowMapping]:
    """Produce a (Proposal, WorkflowMapping) pair for the given GapReport.

    Args:
        report: analyzer output.
        board: board adapter — used for its ``kind`` and ``url``; also for
            the ``add_status`` call when mode=="apply".
        existing: the current mapping (if any) whose aliases and non-gap
            events are merged into the result.
        mode: one of ``"auto"``, ``"interactive"``, ``"apply"``, ``"report"``.
            ``"auto"`` picks interactive iff stdin is a TTY, else report.
        reader: testing hook for non-``input()`` input stream.

    Returns:
        ``(Proposal, WorkflowMapping)`` — mapping reflects the proposal
        applied on top of *existing*.
    """
    resolved_mode = _resolve_mode(mode)
    proposal = Proposal()

    base = WorkflowMapping(
        board_kind=getattr(board, "kind", "github_project"),
        board_url=getattr(board, "url", report.board_url),
        events=dict(existing.events) if existing else {},
        aliases=dict(existing.aliases) if existing else {},
    )

    # Seed mapping with matches from the fuzzy pass.
    for evt, st in report.matched.items():
        base.events.setdefault(evt, st.name)

    if report.is_clean:
        return proposal, base

    # ── Ambiguous first — they have candidates to pick from ───────────────
    for evt, candidates in report.ambiguous.items():
        if resolved_mode == "report":
            proposal.to_skip.append(evt)
            continue
        if resolved_mode == "apply":
            # Non-interactive: pick the first candidate deterministically.
            pick = candidates[0]
            proposal.to_map[evt] = pick
            base.events[evt] = pick.name
            continue
        pick = _resolve_ambiguous(evt, candidates, reader)
        if pick:
            proposal.to_map[evt] = pick
            base.events[evt] = pick.name
        else:
            proposal.to_skip.append(evt)

    # ── Missing ───────────────────────────────────────────────────────────
    for evt in report.missing:
        if resolved_mode == "report":
            proposal.to_skip.append(evt)
            continue
        if resolved_mode == "apply":
            # Attempt add_status — swallow NotImplementedError and skip.
            try:
                new_st = board.add_status(_humanize(evt))
                proposal.to_add.append(evt)
                base.events[evt] = new_st.name
                report.board_statuses.append(new_st)
            except NotImplementedError as exc:
                log.warning("apply mode: board doesn't support add_status for %s — skipping (%s)", evt, exc)
                proposal.to_skip.append(evt)
            continue
        # Interactive path
        action, picked = _resolve_missing(evt, report.board_statuses, reader)
        if action == "add":
            try:
                new_st = board.add_status(_humanize(evt))
                proposal.to_add.append(evt)
                base.events[evt] = new_st.name
                report.board_statuses.append(new_st)
            except NotImplementedError as exc:
                print(f"    (board does not support adding statuses: {exc})")
                proposal.to_skip.append(evt)
        elif action == "map" and picked:
            proposal.to_map[evt] = picked
            base.events[evt] = picked.name
        else:
            proposal.to_skip.append(evt)

    return proposal, base


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_mode(mode: str) -> str:
    if mode == "auto":
        return "interactive" if _is_tty() else "report"
    if mode in ("interactive", "apply", "report"):
        return mode
    raise ValueError(f"unknown mode {mode!r} — use one of: auto, interactive, apply, report")


def _humanize(event: str) -> str:
    """Convert event key to a human column name ('picked_up' → 'Picked Up')."""
    return " ".join(part.capitalize() for part in event.replace("_", " ").split())
