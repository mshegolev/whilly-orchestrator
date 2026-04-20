"""High-level workflow analyzer.

Composes :mod:`whilly.workflow.mapper` + a concrete :class:`BoardSink` into
a single entry point that:

1. Loads any existing ``.whilly/workflow.json`` so user overrides survive.
2. Pulls live board statuses.
3. Runs fuzzy matching → :class:`~whilly.workflow.base.GapReport`.
4. Renders a human-readable summary for CLI / interactive use.

The analyzer *never* mutates the board or the mapping file — that's the
proposer's job. Separation means we can run analysis in CI without any
write scopes on the GitHub token.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from whilly.workflow.base import BoardSink, GapReport, WorkflowMapping
from whilly.workflow.mapper import match_events
from whilly.workflow.registry import known_events

log = logging.getLogger("whilly.workflow.analyzer")


DEFAULT_MAPPING_PATH = Path(".whilly") / "workflow.json"


# ── Mapping file I/O ─────────────────────────────────────────────────────────


def load_mapping(path: Path | str | None = None) -> WorkflowMapping | None:
    """Load an existing mapping file, returning ``None`` if absent.

    Validation is minimal — the loader is permissive. Corrupted files log a
    warning and return ``None`` rather than crashing the analyzer.
    """
    p = Path(path) if path is not None else DEFAULT_MAPPING_PATH
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("failed to read mapping file %s: %s", p, exc)
        return None
    return WorkflowMapping.from_dict(data)


def save_mapping(mapping: WorkflowMapping, path: Path | str | None = None) -> Path:
    """Write a mapping as JSON, creating parent dirs. Returns the resolved path."""
    p = Path(path) if path is not None else DEFAULT_MAPPING_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


# ── The analyzer ─────────────────────────────────────────────────────────────


def analyze(
    board: BoardSink,
    mapping: WorkflowMapping | None = None,
    events: list[str] | None = None,
) -> GapReport:
    """Run the full analysis pass.

    Args:
        board: concrete :class:`BoardSink` — raises on transport failure.
        mapping: optional pre-loaded :class:`WorkflowMapping`; when present,
            explicit event assignments short-circuit fuzzy matching.
        events: events to analyse — defaults to every registered event.

    Returns:
        :class:`GapReport` — the human-printable summary lives in
        :func:`format_report`.
    """
    statuses = board.list_statuses()
    evt_list = events if events is not None else list(known_events().keys())
    board_url = getattr(board, "url", "")
    return match_events(board_url=board_url, statuses=statuses, events=evt_list, mapping=mapping)


# ── Report rendering ─────────────────────────────────────────────────────────


def format_report(report: GapReport, title: str | None = None) -> str:
    """Produce a plain-text summary suitable for stdout.

    Example output::

        Project: https://github.com/users/foo/projects/4
        Status field: 6 options (Todo, In Progress, In Review, Done, Blocked, Failed)

        Lifecycle mapping:
          ready        → 'Todo'          ✓ matched
          picked_up    → 'In Progress'   ✓ matched
          in_review    → 'In Review'     ✓ matched
          done         → 'Done'          ✓ matched
          refused      → 'Blocked'       ✓ matched
          failed       → 'Failed'        ✓ matched

        Clean — no gaps.
    """
    lines: list[str] = []
    if title:
        lines.append(title)
    if report.board_url:
        lines.append(f"Project: {report.board_url}")
    names = ", ".join(s.name for s in report.board_statuses) or "(none)"
    lines.append(f"Status field: {len(report.board_statuses)} options ({names})")
    lines.append("")
    lines.append("Lifecycle mapping:")
    width = max((len(e) for e in known_events()), default=10)
    all_events = list(known_events().keys())
    # Preserve registry order in the output regardless of matched/missing/ambiguous order.
    for event in all_events:
        if event in report.matched:
            lines.append(f"  {event:<{width}} → {report.matched[event].name!r:<20} ✓ matched")
        elif event in report.missing:
            lines.append(f"  {event:<{width}} → {'—':<20} ✗ missing")
        elif event in report.ambiguous:
            names = ", ".join(s.name for s in report.ambiguous[event])
            lines.append(f"  {event:<{width}} → {'(multiple)':<20} ⚠ ambiguous: {names}")
    lines.append("")
    if report.is_clean:
        lines.append("Clean — no gaps.")
    else:
        n_miss = len(report.missing)
        n_amb = len(report.ambiguous)
        parts = []
        if n_miss:
            parts.append(f"{n_miss} missing")
        if n_amb:
            parts.append(f"{n_amb} ambiguous")
        lines.append(f"Gaps: {', '.join(parts)}. Run the proposer to resolve.")
    return "\n".join(lines)
