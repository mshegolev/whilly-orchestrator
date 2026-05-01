<!--
Canonical structural template for Whilly's end-of-run Markdown summary.

This file is the v4.3.1+ baseline that ``VAL-CROSS-BACKCOMPAT-908`` diffs
against. Future Reporter outputs (`whilly/reporter.py::generate_summary`)
MUST preserve the headings + section names below so external dashboards
that regex-extract from `.planning/reports/whilly_summary_*.md` don't
break across releases.

Source-of-truth generator: `whilly/reporter.py::generate_summary`. If
that function changes the heading set, update this baseline in the
SAME commit and bump the major version of the baseline.

The template uses ``<placeholder>`` notation for values that vary per
run; the baseline assertion only cares about structural elements
(headings, table column ordering, section presence), NOT placeholder
values.
-->

# Whilly Cost Report

**Generated:** <UTC-timestamp e.g. 2026-04-29 19:03:23 UTC>
**Plans executed:** <integer>

## Summary

| Metric | Value |
|--------|-------|
| Total iterations | <integer> |
| Total duration | <human-readable, e.g. 1h02m / 5m30s / 12s> |
| Tasks completed | <integer> |
| Input tokens | <human-readable, e.g. 1.2K / 3.4M> |
| Output tokens | <human-readable> |
| Cache read | <human-readable> |
| Cache create | <human-readable> |
| **Total cost** | **$<float, 4 decimals>** |

## Plans

| Plan | Project | Iters | Duration | Tasks | In | Out | Cost |
|------|---------|-------|----------|-------|----|-----|------|
| `<plan_file path>` | <project> | <iters> | <human-duration> | <done>/<total> | <in> | <out> | $<cost> |

<!--
Required structural elements (asserted by tests/unit/test_m1_doc_strings.py):

  - Top-level heading exactly: ``# Whilly Cost Report``
  - Section heading exactly:  ``## Summary``
  - Section heading exactly:  ``## Plans``

Optional / nice-to-have (not asserted, but Reporter currently emits):

  - ``**Generated:**`` line with a UTC timestamp.
  - ``**Plans executed:**`` line with the count.
  - The ``Summary`` table column ordering ``Metric | Value``.
  - The ``Plans`` table column ordering ``Plan | Project | Iters |
    Duration | Tasks | In | Out | Cost``.

Backcompat policy:

  - Adding a NEW heading or row anywhere is allowed (additive).
  - REMOVING any heading listed under "Required structural elements"
    or RENAMING it requires a major-version baseline bump and a
    coordinated update to every external dashboard that consumes the
    file (see ``docs/Distributed-Setup.md`` audit-reports section for
    the canonical mirrors policy).
-->
