"""Fuzzy matching: lifecycle events ↔ board status names.

The matcher is intentionally boring:

* case-insensitive
* exact-equal wins over substring
* alias table (from :mod:`whilly.workflow.registry`) provides synonyms
* any merge with :class:`~whilly.workflow.base.WorkflowMapping` aliases
  takes precedence (user override)

A single pass classifies each event into matched / missing / ambiguous —
see :func:`match_events`. Returned structures feed the
:class:`~whilly.workflow.base.GapReport` directly.
"""

from __future__ import annotations

from collections.abc import Iterable

from whilly.workflow.base import BoardStatus, GapReport, WorkflowMapping
from whilly.workflow.registry import known_events


def _normalize(text: str) -> str:
    """Lowercase + strip — only normalisation we do. Accents/punctuation left
    intact so the user's exact board column names still win on exact match."""
    return (text or "").strip().lower()


def _event_aliases(event: str, user_aliases: dict[str, list[str]] | None = None) -> list[str]:
    """Combine built-in aliases (registry) with user overrides.

    User aliases win but never *replace* — they extend the alias list so
    the event keyword itself stays matchable.
    """
    merged = list(known_events().get(event, []))
    if user_aliases and event in user_aliases:
        merged.extend(user_aliases[event])
    # The event name itself is always a valid alias ("done" matches "done").
    if event not in merged:
        merged.append(event)
    return merged


def _match_one(event: str, statuses: Iterable[BoardStatus], aliases: list[str]) -> list[BoardStatus]:
    """Return all statuses whose name matches any alias for *event*.

    Matching order (first wins for each status):

    1. Exact-equal on normalised names (``"done" == "Done"``).
    2. Alias is a substring of status name (``"in progress"`` ⊂ ``"In Progress"``).
    3. Status name is a substring of alias (handles short labels like ``"WIP"``).

    Multiple statuses can match — that's the *ambiguous* case. Callers
    (see :func:`match_events`) resolve by cardinality.
    """
    alias_norm = [_normalize(a) for a in aliases if a and a.strip()]
    hits: list[BoardStatus] = []
    for st in statuses:
        name = _normalize(st.name)
        if not name:
            continue
        if name in alias_norm:
            hits.append(st)
            continue
        if any(a in name for a in alias_norm if len(a) >= 3):
            hits.append(st)
            continue
        if any(name in a for a in alias_norm if len(name) >= 3):
            hits.append(st)
    # Deduplicate preserving order (Status ids should already be unique;
    # belt+braces in case the board returns dupes).
    seen: set[str] = set()
    unique: list[BoardStatus] = []
    for st in hits:
        if st.id in seen:
            continue
        seen.add(st.id)
        unique.append(st)
    return unique


def match_events(
    board_url: str,
    statuses: list[BoardStatus],
    events: Iterable[str] | None = None,
    mapping: WorkflowMapping | None = None,
) -> GapReport:
    """Classify every registered (or user-supplied) event into a GapReport.

    Args:
        board_url: used only for reporting (passed into GapReport).
        statuses: live board columns, from
            :meth:`~whilly.workflow.base.BoardSink.list_statuses`.
        events: events to analyse. Defaults to every name in
            :func:`~whilly.workflow.registry.known_events`.
        mapping: optional :class:`~whilly.workflow.base.WorkflowMapping`
            with user-defined aliases and *explicit event → status name
            assignments*. Explicit assignments short-circuit fuzzy matching
            (the user has already decided).
    """
    evt_list = list(events) if events is not None else list(known_events().keys())
    user_aliases = mapping.aliases if mapping else None
    explicit = mapping.events if mapping else {}

    status_by_norm_name = {_normalize(s.name): s for s in statuses}

    report = GapReport(board_url=board_url, board_statuses=list(statuses))

    for event in evt_list:
        if event in explicit:
            target_name = _normalize(explicit[event])
            hit = status_by_norm_name.get(target_name)
            if hit:
                report.matched[event] = hit
                continue
            # Explicit mapping points at a non-existent status — treat as
            # missing so the proposer can re-ask.
            report.missing.append(event)
            continue

        aliases = _event_aliases(event, user_aliases)
        hits = _match_one(event, statuses, aliases)
        if len(hits) == 1:
            report.matched[event] = hits[0]
        elif len(hits) == 0:
            report.missing.append(event)
        else:
            report.ambiguous[event] = hits

    return report
