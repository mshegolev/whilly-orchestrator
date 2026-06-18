#!/usr/bin/env python3
"""Model-free core of the single-spec semantic drift detection engine.

This is **standalone repo tooling** (like ``scripts/audit-coverage-matrix.py``),
NOT part of the importable ``whilly/`` package. Per the Phase 30 CONTEXT
decision, anything under ``whilly/`` carries a coverage-matrix entry plus an
``opsx`` capability-spec obligation; a drift checker living there would have to
spec itself. As pure tooling under ``scripts/`` this module is exempt and ships
zero ``whilly/`` behavior change and no new spec obligation.

The functions here are deterministic and free of any model/network/filesystem
side effects beyond an explicit, injectable matrix-file read:

- ``resolve_modules_for_slug`` derives a capability slug's reviewed module set
  live from ``openspec/COVERAGE-MATRIX.md`` (DETECT-04).
- ``build_review_prompt`` is a PURE function assembling the deterministic review
  prompt embedding the spec text + mapped module sources, instructing file:line
  evidence and code-bug | spec-overstatement triage (DETECT-02, DETECT-03).
- ``parse_findings`` / ``validate_finding`` perform robust JSON extraction
  (fence-strip + lazy ``json_repair`` fallback) and per-finding schema
  validation against a single shared schema (DETECT-02, DETECT-03).

Plan 02 wires these into a CLI + live Claude reviewer; this module never invokes
a model or subprocess itself.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone

DEFAULT_MODEL = "claude-opus-4-6[1m]"

# Tool version for the self-describing run metadata block (RUN-03). Bumped
# independently of the whilly package version since this is standalone tooling.
TOOL_VERSION = "31.1.0"

# ---------------------------------------------------------------------------
# Shared schema — single source of truth for both the prompt and the validator.
# ---------------------------------------------------------------------------

FINDING_KEYS: tuple[str, ...] = (
    "severity",
    "slug",
    "requirement",
    "drift",
    "evidence",
    "triage",
    "rationale",
)
SEVERITIES: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")
TRIAGE_VALUES: tuple[str, ...] = ("code-bug", "spec-overstatement")

DEFAULT_MATRIX_PATH = "openspec/COVERAGE-MATRIX.md"

# ---------------------------------------------------------------------------
# Phase 31: capability clusters (RUN-01)
# ---------------------------------------------------------------------------

# Canonical disjoint 6-cluster partition of the 32 live capability slugs
# (verified exhaustive + disjoint against openspec/specs/* by the test suite).
# This is the reporting / parallelism grouping; the set of specs to review is
# still derived live from the filesystem. 7 + 5 + 5 + 5 + 5 + 5 = 32.
CLUSTERS: dict[str, list[str]] = {
    "orchestration": [
        "orchestration-loop",
        "agent-dispatch",
        "batch-planning",
        "result-collection",
        "worktree-isolation",
        "plan-json-contract",
        "task-model-fsm",
    ],
    "prd-decision": [
        "prd-generation",
        "prd-wizard",
        "decision-gate",
        "task-generation",
        "decomposition",
    ],
    "integrations": [
        "github-integration",
        "gitlab-integration",
        "jira-integration",
        "jira-watcher-daemon",
        "mcp-integration",
    ],
    "operator-surface": [
        "cli-surface",
        "dashboard-tui",
        "web-status-ui",
        "operator-views-logs",
        "reporting",
    ],
    "platform": [
        "state-persistence",
        "configuration",
        "scheduling",
        "self-update-doctor",
        "auth-security",
    ],
    "safety-quality": [
        "verification-gates",
        "budget-resource-guards",
        "quality-compliance-audit",
        "recovery-self-healing",
        "notifications",
    ],
}

# Reverse index slug -> cluster, built once at import time.
_SLUG_TO_CLUSTER: dict[str, str] = {slug: cluster for cluster, slugs in CLUSTERS.items() for slug in slugs}


def cluster_for_slug(slug: str) -> str | None:
    """Return the owning cluster name for ``slug``, or ``None`` if unknown.

    Pinned behavior: never raises. An unknown slug (not present in any cluster)
    returns ``None`` so callers can record it as an out-of-partition unit.
    """
    return _SLUG_TO_CLUSTER.get(slug)


def live_slugs(specs_root: str = "openspec/specs") -> set[str]:
    """Enumerate the live capability slug set from the filesystem.

    Returns the set of directory names under ``specs_root`` that contain a
    ``spec.md``. ``specs_root`` is injectable so tests can point at a fixture;
    the partition test asserts against the REAL ``openspec/specs`` so CLUSTERS
    cannot silently drift from the 32 specs. Returns an empty set if the root
    is missing (never raises).
    """
    if not os.path.isdir(specs_root):
        return set()
    return {name for name in os.listdir(specs_root) if os.path.isfile(os.path.join(specs_root, name, "spec.md"))}


# ---------------------------------------------------------------------------
# Task 1: matrix-driven module resolution (DETECT-04)
# ---------------------------------------------------------------------------


def _parse_matrix_rows(matrix_path: str) -> list[tuple[str, str]]:
    """Parse the coverage-matrix table into ``(module, capability)`` tuples.

    Ports the table-parsing approach in
    ``scripts/audit-coverage-matrix.py::parse_coverage_matrix``: regex-locate the
    ``| Module | Capability | Notes |`` table, split each body row on ``|``,
    strip, and skip the header and separator rows. Returns rows in matrix order.
    Returns ``[]`` if the file or table is absent (never raises).
    """
    if not os.path.exists(matrix_path):
        return []

    with open(matrix_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Capture only the coverage-matrix table body (stops at the first blank line,
    # so unrelated later tables in the file are not picked up).
    matrix_section = re.search(r"\| Module \| Capability \| Notes \|(.*?)\n\n", content, re.DOTALL)
    if not matrix_section:
        return []

    rows: list[tuple[str, str]] = []
    for line in matrix_section.group(1).strip().split("\n"):
        line = line.strip()
        # Skip empty lines, separator rows, and the header row.
        if not line or line.startswith("|----") or line.startswith("| Module"):
            continue
        parts = [part.strip() for part in line.split("|") if part.strip()]
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def resolve_modules_for_slug(slug: str, matrix_path: str = DEFAULT_MATRIX_PATH) -> list[str]:
    """Resolve a capability ``slug`` to its mapped ``whilly/`` module paths.

    Derives the review set live from the coverage matrix by EXACT capability
    match (no substring matching). Module paths are returned in matrix order.
    An unknown slug returns ``[]`` without raising. ``matrix_path`` is injectable
    so callers/tests can point at an alternate matrix file.
    """
    return [module for module, capability in _parse_matrix_rows(matrix_path) if capability == slug]


# ---------------------------------------------------------------------------
# Task 2: pure review-prompt builder (DETECT-02 / DETECT-03 prompt contract)
# ---------------------------------------------------------------------------


def build_review_prompt(slug: str, spec_text: str, module_sources: list[tuple[str, str]]) -> str:
    """Assemble the deterministic semantic-drift review prompt (PURE function).

    Takes already-loaded inputs — the caller performs all I/O. This function does
    no ``open()`` and no subprocess work, so identical inputs always produce a
    byte-identical prompt string.

    The prompt instructs the reviewer to compare every ``SHALL`` / ``MUST``
    requirement in ``spec_text`` against the mapped module sources, and to emit a
    strict JSON array of findings using the shared schema (``FINDING_KEYS``),
    severities (``SEVERITIES``), and triage values (``TRIAGE_VALUES``). A clean
    spec returns an empty array ``[]``.
    """
    severities = " | ".join(SEVERITIES)
    triage_values = " | ".join(TRIAGE_VALUES)

    source_blocks = []
    for path, source in module_sources:
        source_blocks.append(f"### FILE: {path}\n{source}")
    sources_section = "\n\n".join(source_blocks)

    schema_lines = [
        '  "severity": one of ' + severities + ",",
        f'  "slug": the capability slug under review (here: "{slug}"),',
        '  "requirement": the exact SHALL/MUST requirement text the drift violates,',
        '  "drift": a one-line description of how the code diverges from the spec,',
        '  "evidence": a "file:line" reference into the module sources above,',
        '  "triage": one of ' + triage_values + ",",
        '  "rationale": why this is a drift and how the triage was chosen',
    ]
    schema_block = "{\n" + "\n".join(schema_lines) + "\n}"

    return (
        "You are a semantic spec-drift reviewer. Compare each normative "
        "requirement (every SHALL / MUST clause) in the OpenSpec capability spec "
        "below against the source code of the modules that implement it, and "
        "report where the code and the spec disagree.\n\n"
        f"## Capability under review: {slug}\n\n"
        "## SPECIFICATION (source of truth for intended behavior)\n\n"
        f"{spec_text}\n\n"
        "## IMPLEMENTING MODULE SOURCES\n\n"
        f"{sources_section}\n\n"
        "## OUTPUT CONTRACT\n\n"
        "Return ONLY a strict JSON array of finding objects — no prose, no "
        "markdown fences. Each finding object MUST have exactly these seven "
        "keys:\n\n"
        f"{schema_block}\n\n"
        f"Allowed severity values: {severities}.\n"
        f"Allowed triage values: {triage_values} "
        "(code-bug = the implementation is wrong; spec-overstatement = the spec "
        "claims more than the code can or should guarantee).\n"
        'Every finding MUST cite concrete "file:line" evidence drawn from the '
        "module sources above.\n\n"
        "If the spec and the code fully agree (no drift), return an empty array: "
        "[]\n"
    )


# ---------------------------------------------------------------------------
# Task 3: findings parse + per-finding validate (DETECT-02, DETECT-03)
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Strip surrounding whitespace and ```json / ``` markdown fences."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    elif text.startswith("```"):
        text = text[len("```") :].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def _extract_json_array(text: str) -> str | None:
    """Return the substring spanning the first top-level ``[...]`` array, if any."""
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return None


def parse_findings(text: str) -> list[dict]:
    """Parse model output into a list of schema-valid finding dicts.

    Robust to dirty output: strips markdown fences, tolerates leading/trailing
    prose by extracting the first ``[...]`` array span, and falls back to the
    OPTIONAL ``json_repair`` dependency (lazy-imported, never hard-imported) when
    strict parsing fails. Phase 30 is report-only and always exits 0, so this
    function NEVER raises — unrecoverable input yields ``[]``.

    After parsing, every entry is filtered through ``validate_finding`` so callers
    only ever see schema-valid findings.
    """
    candidate = _strip_fences(text)

    data = _try_load(candidate)
    if data is None:
        extracted = _extract_json_array(candidate)
        if extracted is not None:
            data = _try_load(extracted)
    if data is None:
        return []

    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and validate_finding(item)]


def _try_load(text: str):
    """Attempt ``json.loads`` then a lazy ``json_repair`` fallback; ``None`` on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import json_repair

        return json_repair.loads(text)
    except Exception:
        return None


def validate_finding(finding: dict) -> bool:
    """Return True iff ``finding`` matches the shared seven-key drift schema.

    Requires exactly the keys in ``FINDING_KEYS``, ``severity`` in ``SEVERITIES``,
    ``triage`` in ``TRIAGE_VALUES``, and a non-empty ``file:line``-shaped
    ``evidence`` string (must contain ``:``).
    """
    if not isinstance(finding, dict):
        return False
    if set(finding.keys()) != set(FINDING_KEYS):
        return False
    if finding["severity"] not in SEVERITIES:
        return False
    if finding["triage"] not in TRIAGE_VALUES:
        return False
    evidence = finding["evidence"]
    if not isinstance(evidence, str) or ":" not in evidence or not evidence.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# Plan 02 Task 1: review_spec pipeline (DETECT-01)
# ---------------------------------------------------------------------------


def review_spec(
    slug: str,
    reviewer: Callable[[str], str],
    *,
    specs_root: str = "openspec/specs",
    repo_root: str = ".",
    matrix_path: str = DEFAULT_MATRIX_PATH,
) -> list[dict]:
    """Review a single capability ``slug`` for semantic drift.

    The end-to-end pipeline (DETECT-01): load the capability's ``spec.md``,
    resolve its mapped ``whilly/`` module set from the coverage matrix, read
    those module sources, build the deterministic review prompt, hand the prompt
    to the injected ``reviewer`` callable, and parse the result into findings.

    ``reviewer`` is the dependency-injection seam: tests pass a fake returning
    canned JSON (no network, no CLI); the default :func:`claude_reviewer` shells
    to the Claude CLI. All filesystem I/O (spec.md + module reads) lives here so
    :func:`build_review_prompt` stays pure.

    Phase 30 is report-only and the CLI always exits 0, so this function NEVER
    raises on bad input: a missing/invalid slug (no ``spec.md`` on disk) logs a
    diagnostic to stderr and returns ``[]`` WITHOUT calling the reviewer. Mapped
    modules that are absent on disk are recorded as unreadable and skipped, not
    fatal. ``reviewer`` output that is not parseable yields ``[]`` (via
    :func:`parse_findings`).
    """
    spec_path = os.path.join(specs_root, slug, "spec.md")
    if not os.path.isfile(spec_path):
        print(
            f"semantic_drift_check: no spec.md for slug {slug!r} at {spec_path!r}; returning []",
            file=sys.stderr,
        )
        return []

    with open(spec_path, "r", encoding="utf-8") as f:
        spec_text = f.read()

    module_paths = resolve_modules_for_slug(slug, matrix_path=matrix_path)
    sources: list[tuple[str, str]] = []
    for module in module_paths:
        abs_path = os.path.join(repo_root, module)
        try:
            with open(abs_path, "r", encoding="utf-8") as mf:
                sources.append((module, mf.read()))
        except OSError:
            # A mapped module missing on disk is non-fatal: record it so the
            # prompt still references the path, and let the reviewer note it.
            print(
                f"semantic_drift_check: mapped module {module!r} unreadable; skipping its source",
                file=sys.stderr,
            )
            sources.append((module, "(source file unreadable / not found)"))

    prompt = build_review_prompt(slug, spec_text, sources)
    raw = reviewer(prompt)
    return parse_findings(raw)


# ---------------------------------------------------------------------------
# Plan 02 Task 2: default Claude-CLI reviewer + --slug CLI main (DETECT-01)
# ---------------------------------------------------------------------------


def claude_reviewer(prompt: str) -> str:
    """Default reviewer: shell to the Claude CLI and return the model text.

    Kept deliberately thin (per Phase 30 CONTEXT — no full retry stack like
    ``whilly/adapters/runner/claude_cli.py``). Resolves the binary via
    ``CLAUDE_BIN`` (default ``claude``), runs ``claude --model <m>
    --disallowedTools ... -p <prompt> --output-format json`` capturing stdout,
    then unwraps the Claude ``--output-format json`` envelope to its inner
    ``result`` text. Falls back to raw stdout if the envelope shape is
    unexpected (e.g. a bare JSON array). Timeout via ``WHILLY_CLAUDE_TIMEOUT``.

    ``--disallowedTools`` mirrors the v4.7 deny-by-default posture so the agent
    cannot try to Write the answer to a file and leave stdout empty.
    """
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    model = os.environ.get("WHILLY_MODEL") or DEFAULT_MODEL
    timeout = int(os.environ.get("WHILLY_CLAUDE_TIMEOUT", "1800"))
    cmd = [
        claude_bin,
        "--model",
        model,
        "--disallowedTools",
        "Write,Edit,MultiEdit,NotebookEdit,Bash",
        "-p",
        prompt,
        "--output-format",
        "json",
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = completed.stdout or ""

    # Unwrap the {"result": "...", ...} envelope; fall back to raw stdout.
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
        return envelope["result"]
    return stdout


# ---------------------------------------------------------------------------
# Phase 31 Task 2: bounded fleet fan-out + run metadata (RUN-01, RUN-02, RUN-03)
# ---------------------------------------------------------------------------


def _severity_index(severity: str) -> int:
    """Rank a severity by ``SEVERITIES`` order; unknown severities sort last."""
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return len(SEVERITIES)


def run_fleet(
    slugs,
    reviewer: Callable[[str], str],
    *,
    max_workers: int = 6,
    specs_root: str = "openspec/specs",
    repo_root: str = ".",
    matrix_path: str = DEFAULT_MATRIX_PATH,
) -> dict:
    """Review every slug in ``slugs`` via a bounded ``ThreadPoolExecutor``.

    Each slug is submitted as one unit of work that calls the existing
    :func:`review_spec` with the injected ``reviewer``. The unit is wrapped in
    try/except so a single failing review (CLI error, parse failure, raising
    reviewer) records a structured ``{slug, cluster, error}`` entry and the
    fleet CONTINUES — a failed unit NEVER aborts the run (RUN-02). After all
    futures complete the findings are flattened and sorted deterministically by
    ``(slug, severity)`` so a fixed set of reviewer responses yields byte-stable
    ordering (RUN-01). The unit of work is a blocking subprocess to the Claude
    CLI in production, so threads are the right primitive.

    Returns a results dict::

        {
            "findings": [...],   # flat, sorted by (slug, severity)
            "errors":   [...],   # [{slug, cluster, error}, ...]
            "reviewed": [...],   # slugs that completed without error
        }
    """
    findings: list[dict] = []
    errors: list[dict] = []
    reviewed: list[str] = []

    def _unit(slug: str):
        return review_spec(
            slug,
            reviewer,
            specs_root=specs_root,
            repo_root=repo_root,
            matrix_path=matrix_path,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_slug = {pool.submit(_unit, slug): slug for slug in slugs}
        for future in concurrent.futures.as_completed(future_to_slug):
            slug = future_to_slug[future]
            cluster = cluster_for_slug(slug)
            try:
                spec_findings = future.result()
            except Exception as exc:  # noqa: BLE001 — per-unit isolation (RUN-02)
                errors.append({"slug": slug, "cluster": cluster, "error": str(exc)})
                continue
            reviewed.append(slug)
            for finding in spec_findings:
                tagged = dict(finding)
                tagged.setdefault("cluster", cluster)
                findings.append(tagged)

    findings.sort(key=lambda f: (f.get("slug", ""), _severity_index(f.get("severity", ""))))
    return {"findings": findings, "errors": errors, "reviewed": reviewed}


def _default_git_info() -> dict:
    """Default git seam: read HEAD commit + dirty flag, degrading on failure.

    Shells ``git rev-parse HEAD`` for the commit and treats a non-empty
    ``git status --porcelain`` as dirty. Wrapped so any subprocess failure
    (no git, not a repo, timeout) degrades to ``commit=None``/``dirty=None``
    rather than raising — git metadata is best-effort, never fatal (RUN-03).
    """
    commit = None
    dirty = None
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if rev.returncode == 0:
            commit = rev.stdout.strip() or None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode == 0:
            dirty = bool(status.stdout.strip())
    except Exception:  # noqa: BLE001 — best-effort, degrade gracefully
        return {"commit": commit, "dirty": dirty}
    return {"commit": commit, "dirty": dirty}


def collect_run_metadata(
    *,
    model: str | None = None,
    git_info: Callable[[], dict] | None = None,
    now: Callable[[], str] | None = None,
    tool_version: str = TOOL_VERSION,
) -> dict:
    """Build the self-describing run-metadata block (RUN-03).

    Resolves ``model`` by precedence: explicit ``model`` arg > ``WHILLY_MODEL``
    env > :data:`DEFAULT_MODEL`. ``git_info`` and ``now`` are injectable seams so
    tests never depend on real git state or the wall clock; the defaults are
    :func:`_default_git_info` (degrades to ``None`` on failure) and an ISO-8601
    UTC timestamp. Returns ``{model, commit, dirty, timestamp, tool_version}``.
    """
    resolved_model = model or os.environ.get("WHILLY_MODEL") or DEFAULT_MODEL
    git = (git_info or _default_git_info)()
    timestamp = (now or (lambda: datetime.now(timezone.utc).isoformat()))()
    return {
        "model": resolved_model,
        "commit": git.get("commit"),
        "dirty": git.get("dirty"),
        "timestamp": timestamp,
        "tool_version": tool_version,
    }


# ---------------------------------------------------------------------------
# Phase 31 Task 3: artifact builder + pure summary formatter (REPORT-01/02)
# ---------------------------------------------------------------------------


def build_artifact(results: dict, metadata: dict, *, total: int = 32) -> dict:
    """Build the CONTEXT-locked JSON artifact from a fleet ``results`` dict.

    Shape (REPORT-01)::

        {
          "run": metadata,
          "coverage": {"reviewed": <n>, "total": 32},
          "clusters": {<cluster>: {high, medium, low, clean, error}},
          "findings": [...],   # the already-sorted flat findings list
          "errors":   [...],   # the per-unit error entries
        }

    Per-cluster tallies are computed by bucketing findings by severity into the
    owning cluster (via the finding's ``cluster`` tag, else :func:`cluster_for_slug`),
    counting error entries into the cluster's ``error`` bucket, and counting
    ``clean`` as reviewed specs in that cluster that produced zero findings and
    had no error. Every one of the six clusters is always present.
    """
    findings = results.get("findings", [])
    errors = results.get("errors", [])
    reviewed = results.get("reviewed", [])

    clusters: dict[str, dict[str, int]] = {
        name: {"high": 0, "medium": 0, "low": 0, "clean": 0, "error": 0} for name in CLUSTERS
    }

    _sev_bucket = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    slugs_with_findings: set[str] = set()
    for finding in findings:
        cluster = finding.get("cluster") or cluster_for_slug(finding.get("slug", ""))
        if cluster not in clusters:
            continue
        bucket = _sev_bucket.get(finding.get("severity", ""))
        if bucket is not None:
            clusters[cluster][bucket] += 1
        slugs_with_findings.add(finding.get("slug", ""))

    error_slugs: set[str] = set()
    for err in errors:
        cluster = err.get("cluster") or cluster_for_slug(err.get("slug", ""))
        if cluster in clusters:
            clusters[cluster]["error"] += 1
        error_slugs.add(err.get("slug", ""))

    # A reviewed spec with no findings and no error is "clean".
    for slug in reviewed:
        cluster = cluster_for_slug(slug)
        if cluster not in clusters:
            continue
        if slug not in slugs_with_findings and slug not in error_slugs:
            clusters[cluster]["clean"] += 1

    return {
        "run": metadata,
        "coverage": {"reviewed": len(reviewed), "total": total},
        "clusters": clusters,
        "findings": findings,
        "errors": errors,
    }


def format_summary(artifact: dict) -> str:
    """Render a human stdout summary from an ``artifact`` dict (PURE function).

    No I/O, no subprocess, no fleet call — buildable and assertable from a
    hand-made dict (REPORT-02). Renders a per-cluster HIGH/MEDIUM/LOW + clean
    table, a coverage ``reviewed/32`` line, and a confirmed-findings (specs with
    >=1 finding) vs clean-specs split.
    """
    clusters = artifact.get("clusters", {})
    coverage = artifact.get("coverage", {})
    findings = artifact.get("findings", [])
    errors = artifact.get("errors", [])

    lines: list[str] = []
    lines.append("Semantic drift sweep")
    reviewed = coverage.get("reviewed", 0)
    total = coverage.get("total", 0)
    lines.append(f"Coverage: reviewed {reviewed}/{total} specs")
    lines.append("")

    header = f"{'cluster':<18} {'HIGH':>5} {'MEDIUM':>7} {'LOW':>5} {'clean':>6} {'error':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for name in CLUSTERS:
        tally = clusters.get(name, {"high": 0, "medium": 0, "low": 0, "clean": 0, "error": 0})
        lines.append(
            f"{name:<18} {tally.get('high', 0):>5} {tally.get('medium', 0):>7} "
            f"{tally.get('low', 0):>5} {tally.get('clean', 0):>6} {tally.get('error', 0):>6}"
        )
    lines.append("-" * len(header))

    confirmed_slugs = {f.get("slug") for f in findings}
    clean_count = sum(t.get("clean", 0) for t in clusters.values())
    lines.append("")
    lines.append(f"Confirmed findings: {len(findings)} across {len(confirmed_slugs)} spec(s)")
    lines.append(f"Clean specs: {clean_count}")
    if errors:
        lines.append(f"Errors: {len(errors)} (see artifact 'errors')")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, reviewer: Callable[[str], str] = claude_reviewer) -> int:
    """CLI entry: review one ``--slug`` or the whole fleet with ``--all``.

    Modes are mutually exclusive and exactly one is required. ``--slug``
    preserves the Phase 30 behavior exactly (print findings JSON to stdout).
    ``--all`` reviews every slug in the :data:`CLUSTERS` partition via
    :func:`run_fleet`, writes the locked JSON artifact to ``--output``, and
    prints :func:`format_summary` to stdout. Always returns exit code 0 —
    severity gating is Phase 32. ``reviewer`` is injectable so tests drive the
    full CLI path with a fake reviewer and no CLI/network.
    """
    parser = argparse.ArgumentParser(
        prog="semantic_drift_check",
        description="Review OpenSpec capability specs for semantic drift against their mapped modules.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--slug", help="review a single capability slug (openspec/specs/<slug>/spec.md)")
    mode.add_argument("--all", action="store_true", help="review the whole fleet (all 32 clusters' specs)")
    parser.add_argument("--specs-root", default="openspec/specs", help="root of capability spec dirs")
    parser.add_argument("--repo-root", default=".", help="repo root for resolving module sources")
    parser.add_argument("--matrix-path", default=DEFAULT_MATRIX_PATH, help="coverage-matrix path")
    parser.add_argument("--max-workers", type=int, default=6, help="fleet thread-pool size (--all)")
    parser.add_argument("--output", default="semantic-drift-findings.json", help="artifact path (--all)")
    parser.add_argument("--model", default=None, help="model recorded in run metadata (--all)")
    args = parser.parse_args(argv)

    if args.all:
        slugs = [slug for slugs in CLUSTERS.values() for slug in slugs]
        results = run_fleet(
            slugs,
            reviewer=reviewer,
            max_workers=args.max_workers,
            specs_root=args.specs_root,
            repo_root=args.repo_root,
            matrix_path=args.matrix_path,
        )
        metadata = collect_run_metadata(model=args.model)
        artifact = build_artifact(results, metadata)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(artifact, f, indent=2)
        print(format_summary(artifact))
        return 0

    findings = review_spec(
        args.slug,
        reviewer=reviewer,
        specs_root=args.specs_root,
        repo_root=args.repo_root,
        matrix_path=args.matrix_path,
    )
    print(json.dumps(findings, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
