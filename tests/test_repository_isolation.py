"""Repository isolation guard (SC-5.3, Block 6).

PRD-wui-multi-plan v2 §SC-5.3 says raw ``FROM plans`` / ``FROM tasks``
SQL should live in the data-access layer (the repository + migrations),
not scattered across new feature modules. This test is a static guard:
it walks ``whilly/**.py`` and asserts every Python file containing the
patterns is on a documented allowlist.

Why an allowlist instead of "repository-only"?
    The v4 codebase already has raw SQL in several non-repository
    modules (control-plane server, CLI tools, dashboard projections).
    Refactoring them is out-of-scope for the wui-multi-plan PRD; the
    pragmatic guard here is to **freeze the surface** so any *new*
    file that grows raw ``FROM plans`` / ``FROM tasks`` SQL is caught
    in CI rather than at code-review time.

How the check works:
    For each ``whilly/**/*.py`` file:
      * read the source text;
      * search (case-insensitive, word-boundaried) for ``FROM plans``
        or ``FROM tasks``;
      * if matches are found and the relative path is NOT on
        :data:`_ALLOWED_PATHS`, fail the test with a clear message
        pointing the operator at the offending lines.

The allowlist lives at the top of this file so adding a justified
exception is one PR review away. The migrations directory is excluded
wholesale — Alembic revision files are *defined* to issue raw DDL/DML.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
WHILLY_ROOT: Path = PROJECT_ROOT / "whilly"
MIGRATIONS_DIR: Path = WHILLY_ROOT / "adapters" / "db" / "migrations"

# Match SQL keyword + table name where the keyword is fully UPPERCASE
# (the project SQL style — see :file:`whilly/adapters/db/repository.py`).
# Lowercase ``into Tasks`` in English prose (docstring / comment) is
# excluded by design: it would not be executable SQL anyway, and
# false-positiving on every English sentence containing "into tasks"
# defeats the static guard. Word-boundary on the right rules out
# ``FROM plans_archive`` / ``FROM tasks_old`` etc.
_RAW_SQL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bFROM\s+plans\b"),
    re.compile(r"\bFROM\s+tasks\b"),
    re.compile(r"\bINTO\s+plans\b"),
    re.compile(r"\bINTO\s+tasks\b"),
    re.compile(r"\bUPDATE\s+plans\b"),
    re.compile(r"\bUPDATE\s+tasks\b"),
    re.compile(r"\bDELETE\s+FROM\s+plans\b"),
    re.compile(r"\bDELETE\s+FROM\s+tasks\b"),
)

#: Files allowed to contain raw ``FROM plans`` / ``FROM tasks`` SQL.
#: Paths are relative to ``whilly/``. The canonical place for new SQL
#: against these tables is :file:`whilly/adapters/db/repository.py`.
#:
#: Existing files predate SC-5.3; they are grandfathered here so the
#: test passes on day one and serves as a forward-looking guard against
#: NEW files introducing the anti-pattern.
_ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        # Canonical data-access layer.
        "adapters/db/repository.py",
        # Control-plane HTTP server — owns the worker RPC surface and
        # plan-reset path that need direct table access.
        "adapters/transport/server.py",
        # Forge intake — the one entry-point that needs to dedupe by
        # ``github_issue_ref`` before the repository INSERT.
        "forge/intake.py",
        # CLI tools that ship as the operator-facing thin clients to
        # the same SQL surface.
        "cli/plan.py",
        "cli/dashboard.py",
        # API routers + projections that issue read-side SQL with
        # correlated subqueries the repository currently does not
        # expose. Migrating these to repository methods is tracked as
        # follow-up work; the allowlist documents the debt.
        "api/plans_api.py",
        "api/tasks_api.py",
        "api/metrics.py",
        # Operator-facing materialised view helpers.
        "operator_views.py",
        # Workflow PR iterator — issues its own SELECT/INSERT against
        # tasks for the targeted-rebuild path.
        "workflow/pr_iterate.py",
    }
)


def _iter_python_files() -> list[Path]:
    """Walk ``whilly/`` and return every ``.py`` file except migrations."""
    out: list[Path] = []
    for path in WHILLY_ROOT.rglob("*.py"):
        # Skip Alembic migrations — they are SQL by design.
        if MIGRATIONS_DIR in path.parents:
            continue
        # Skip __pycache__ and similar generated dirs.
        if "__pycache__" in path.parts:
            continue
        out.append(path)
    return out


def _find_violations(file_path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line_no, pattern, line_text)`` triples for each match."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Comments and docstring narrative inside .py files often quote
        # SQL for documentation purposes — skip them so the test does
        # not fire on prose. We only care about executable string
        # literals, which by convention are NOT comment lines.
        if stripped.startswith("#"):
            continue
        for pattern in _RAW_SQL_PATTERNS:
            if pattern.search(line):
                hits.append((line_no, pattern.pattern, line.rstrip()))
                break  # one match per line is enough
    return hits


def test_no_unauthorized_raw_plans_or_tasks_sql() -> None:
    """Every ``whilly/**.py`` containing raw plans/tasks SQL must be on the allowlist.

    Failure message lists each unauthorised file + the offending lines
    so the operator can either move the SQL into the repository or add
    an explicit allowlist entry with a code-review-visible justification.
    """
    unauthorised: dict[str, list[tuple[int, str, str]]] = {}
    for path in _iter_python_files():
        rel = path.relative_to(WHILLY_ROOT).as_posix()
        if rel in _ALLOWED_PATHS:
            continue
        violations = _find_violations(path)
        if violations:
            unauthorised[rel] = violations

    if unauthorised:
        lines: list[str] = [
            "Unauthorised raw FROM plans / FROM tasks SQL detected (SC-5.3).",
            "Move the SQL into whilly/adapters/db/repository.py or add the",
            "file to _ALLOWED_PATHS in tests/test_repository_isolation.py with",
            "a justification in the PR description.",
            "",
        ]
        for rel, hits in sorted(unauthorised.items()):
            lines.append(f"  {rel}:")
            for line_no, pattern, text in hits:
                lines.append(f"    line {line_no} (match: {pattern}): {text}")
        pytest_msg = "\n".join(lines)
        raise AssertionError(pytest_msg)


def test_allowlist_paths_exist() -> None:
    """Every path on the allowlist must actually exist on disk.

    Prevents the allowlist from rotting silently as files get renamed
    or removed. A missing path becomes a loud test failure rather
    than a slow "the guard isn't enforcing anything" leak.
    """
    missing = [rel for rel in _ALLOWED_PATHS if not (WHILLY_ROOT / rel).is_file()]
    assert not missing, f"_ALLOWED_PATHS contains entries that don't exist: {missing}"


def test_allowlist_paths_actually_contain_raw_sql() -> None:
    """Every allowlisted file must actually contain a raw SQL hit.

    If a file on the allowlist has no raw SQL, the entry is dead weight
    and should be removed — keeping the allowlist narrow makes the
    guard meaningful.
    """
    no_hits: list[str] = []
    for rel in _ALLOWED_PATHS:
        path = WHILLY_ROOT / rel
        if not path.is_file():
            continue
        if not _find_violations(path):
            no_hits.append(rel)
    assert not no_hits, f"_ALLOWED_PATHS contains entries with NO raw SQL hits — remove them: {no_hits}"
