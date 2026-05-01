"""Static-doc consistency check for ``whilly worker connect http://...`` examples.

Background
----------
The ``whilly-worker`` (and the inner ``whilly worker connect``) URL-scheme
guard at :func:`whilly.cli.worker.enforce_scheme_guard` rejects plain
HTTP to a non-loopback host unless the operator explicitly passes
``--insecure``. See ``VAL-M1-INSECURE-001..009`` in the mission's
validation contract.

Therefore, any documentation example of the literal phrase
``whilly worker connect http://...`` must satisfy at least one of:

(i)  the URL targets a loopback host (``127.*`` / ``localhost`` / ``::1``), so
     the guard is auto-satisfied without ``--insecure``; or
(ii) the same line, OR the same fenced code block, also contains
     ``--insecure`` so a copy-paste-er actually runs the documented
     command without hitting the guard.

If a doc example violates this, the M1 quickstart fails out of the box
for every reader who doesn't already know about ``--insecure``. This
test pins that consistency across every committed Markdown file in the
repo.

Scope
-----
We deliberately scan *all* committed ``*.md`` files (excluding hidden
``.planning/`` / ``.git/`` and dependency / venv directories) so that
any future doc that gets a copy-pasted ``whilly worker connect http://``
example is covered automatically — not just the three files that
existed at the time this test was authored.

The match is line-anchored: we look for the exact substring
``whilly worker connect http://`` (NOT ``https://`` — those satisfy the
scheme guard intrinsically). Code-block boundaries are detected via
``` fences (the canonical Markdown convention used throughout this
repo's docs).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Top-level dirs to skip when walking for *.md files. Hidden dirs are
# also skipped via the leading-dot check in :func:`_iter_markdown_files`.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "site-packages",
        "__pycache__",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

# The literal we care about. We deliberately do not match ``https://`` —
# the worker scheme guard exempts HTTPS regardless of ``--insecure``.
_CONNECT_HTTP: str = "whilly worker connect http://"

# Loopback hostnames recognised by :func:`whilly.cli.worker._is_loopback_host`.
# IPv4: anything in 127.0.0.0/8.  IPv6: ``[::1]``. Names: ``localhost``.
# We accept the canonical and bracketed IPv6 forms.
_LOOPBACK_HOST_RE = re.compile(
    r"http://(?:"
    r"127(?:\.\d{1,3}){3}"  # 127.0.0.0/8
    r"|localhost"
    r"|\[::1\]"
    r")(?::\d+)?(?:/|\s|$)",
    re.IGNORECASE,
)


def _iter_markdown_files(root: Path) -> Iterable[Path]:
    """Yield every committed ``*.md`` file under ``root``.

    Skips hidden directories (anything starting with ``.``) and the
    common build / venv / cache paths in :data:`_SKIP_DIRS`. This keeps
    the scan focused on operator-facing docs and excludes mission-local
    untracked content (``.planning/``) plus dependency trees.
    """
    for path in root.rglob("*.md"):
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts[:-1]):
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _split_into_code_blocks(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start_line, end_line, body)`` for every fenced code block.

    Line numbers are 1-based and inclusive — they correspond to the
    fence lines themselves so the body lines are ``start_line+1 ..
    end_line-1``. We keep the simple state-machine approach rather than
    pulling in a full Markdown parser because the docs in this repo use
    only ``` fences (no ``~~~``, no indented code blocks containing CLI
    commands) — and a strict triple-backtick scan is sufficient for the
    consistency invariant we want to assert.
    """
    blocks: list[tuple[int, int, str]] = []
    in_block = False
    block_start = 0
    block_lines: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_block:
                in_block = True
                block_start = idx
                block_lines = []
            else:
                blocks.append((block_start, idx, "\n".join(block_lines)))
                in_block = False
        elif in_block:
            block_lines.append(line)
    return blocks


def _block_for_line(blocks: list[tuple[int, int, str]], lineno: int) -> tuple[int, int, str] | None:
    """Return the ``(start, end, body)`` triple containing ``lineno``, or ``None``."""
    for start, end, body in blocks:
        if start < lineno < end:
            return (start, end, body)
    return None


def _is_loopback_url_match(line: str) -> bool:
    """Does the ``whilly worker connect http://...`` URL on ``line`` target loopback?

    We re-anchor on the literal ``http://`` after the command so trailing
    angle-bracket placeholders (``<vps-ip>``) are NOT misclassified as
    loopback.
    """
    return _LOOPBACK_HOST_RE.search(line) is not None


def _line_or_block_has_insecure(line: str, block_body: str | None) -> bool:
    """``--insecure`` appears either on the same line, or anywhere in the same code block."""
    if "--insecure" in line:
        return True
    if block_body is not None and "--insecure" in block_body:
        return True
    return False


def _collect_violations(root: Path) -> list[str]:
    """Walk every Markdown file and collect ``whilly worker connect http://`` violations.

    A violation is a line that:
      1. contains the literal ``whilly worker connect http://`` substring, AND
      2. does NOT target a loopback host on that same line, AND
      3. does NOT have ``--insecure`` on that line OR in the same fenced
         code block.

    Returns a list of human-readable ``"path:lineno: line"`` strings.
    """
    violations: list[str] = []
    for path in _iter_markdown_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Skip unreadable files — non-utf8 docs would fail their own
            # text-content checks elsewhere; we don't double-report here.
            continue
        if _CONNECT_HTTP not in text:
            continue
        blocks = _split_into_code_blocks(text)
        for idx, line in enumerate(text.splitlines(), start=1):
            if _CONNECT_HTTP not in line:
                continue
            if _is_loopback_url_match(line):
                continue
            block = _block_for_line(blocks, idx)
            block_body = block[2] if block else None
            if _line_or_block_has_insecure(line, block_body):
                continue
            rel = path.relative_to(root)
            violations.append(f"{rel}:{idx}: {line.rstrip()}")
    return violations


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


def test_repo_root_is_resolvable() -> None:
    """Sanity: the test correctly locates the repo root.

    This both pins the assumption used by the rest of the suite and
    yields a clear failure message if the test file is moved.
    """
    assert (REPO_ROOT / "pyproject.toml").is_file(), (
        f"could not locate pyproject.toml under inferred REPO_ROOT={REPO_ROOT}; "
        "the test file may have been moved without updating the parents[...] index"
    )


def test_connect_examples_use_insecure_or_loopback() -> None:
    """Every ``whilly worker connect http://...`` example is copy-paste-runnable.

    Asserts the M1 invariant from the worker scheme guard:
    plain-HTTP-to-non-loopback examples MUST include ``--insecure``
    on the same line or in the same fenced code block, otherwise
    a reader following the doc verbatim hits ``InsecureSchemeError``.
    """
    violations = _collect_violations(REPO_ROOT)
    assert not violations, (
        "found `whilly worker connect http://...` example(s) that target a non-loopback "
        "host without `--insecure` on the same line or in the same code block — these will "
        "fail out of the box because whilly/cli/worker.py rejects plain HTTP to non-loopback "
        "hosts unless --insecure is set:\n  " + "\n  ".join(violations)
    )


def test_helper_classifies_loopback_url_correctly() -> None:
    """Direct unit test for :func:`_is_loopback_url_match` so the consistency
    test's gating logic is itself covered.
    """
    assert _is_loopback_url_match("whilly worker connect http://127.0.0.1:8000")
    assert _is_loopback_url_match("whilly worker connect http://127.42.1.1:8000")
    assert _is_loopback_url_match("whilly worker connect http://localhost:8000")
    assert _is_loopback_url_match("whilly worker connect http://[::1]:8000")
    # Non-loopback must NOT match.
    assert not _is_loopback_url_match("whilly worker connect http://vps.example.com:8000")
    assert not _is_loopback_url_match("whilly worker connect http://192.168.1.10:8000")
    assert not _is_loopback_url_match("whilly worker connect http://10.0.0.5:8000")
    assert not _is_loopback_url_match("whilly worker connect http://<vps-ip>:8000")


def test_helper_detects_insecure_in_block_but_not_on_line() -> None:
    """``--insecure`` may appear later in the same code block (e.g. on a
    continuation line after a ``\\`` line-continuation).
    """
    block_body = (
        "whilly worker connect http://vps.example.com:8000 \\\n"
        "    --bootstrap-token X \\\n"
        "    --plan demo \\\n"
        "    --insecure\n"
    )
    line = "whilly worker connect http://vps.example.com:8000 \\"
    assert not _is_loopback_url_match(line)
    assert _line_or_block_has_insecure(line, block_body)


def test_helper_block_split_pairs_fences() -> None:
    """Sanity: code-block detection pairs opening and closing fences.

    Catches off-by-one regressions in ``_split_into_code_blocks`` that
    would otherwise let the consistency test silently pass even when
    ``--insecure`` is in a *different* fenced block from the URL.
    """
    text = "intro\n```bash\nfoo\nbar\n```\nbetween\n```\nbaz\n```\n"
    blocks = _split_into_code_blocks(text)
    assert len(blocks) == 2
    # First block: fence at lines 2 and 5; body is lines 3-4.
    assert blocks[0][0] == 2 and blocks[0][1] == 5
    assert "foo" in blocks[0][2] and "bar" in blocks[0][2]
    # Second block: fence at lines 7 and 9; body is line 8.
    assert blocks[1][0] == 7 and blocks[1][1] == 9
    assert "baz" in blocks[1][2]


@pytest.mark.parametrize(
    "doc_relpath",
    [
        "README.md",
        "docs/Distributed-Setup.md",
        "DEMO.md",
    ],
)
def test_known_quickstart_docs_each_show_insecure_for_non_loopback(doc_relpath: str) -> None:
    """The three M1 quickstart docs all carry the non-loopback example
    by design (per feature ``fix-m1-docs-non-loopback-http-insecure``).
    Each one MUST include the ``--insecure`` flag in the same code
    block as the connect example, since none of them targets loopback.

    This is a more focused assertion than
    :func:`test_connect_examples_use_insecure_or_loopback` — it pins
    the *specific* docs that the feature description called out, so
    accidental deletion of the warning won't be masked by a future
    refactor that moves the example elsewhere.
    """
    path = REPO_ROOT / doc_relpath
    assert path.is_file(), f"expected M1 quickstart doc at {doc_relpath} to exist"
    text = path.read_text(encoding="utf-8")
    blocks = _split_into_code_blocks(text)
    found_match = False
    for idx, line in enumerate(text.splitlines(), start=1):
        if _CONNECT_HTTP not in line:
            continue
        found_match = True
        if _is_loopback_url_match(line):
            continue
        block = _block_for_line(blocks, idx)
        block_body = block[2] if block else None
        assert _line_or_block_has_insecure(line, block_body), (
            f"{doc_relpath}:{idx}: non-loopback `whilly worker connect http://...` example "
            f"is missing `--insecure` in the same line or code block: {line.rstrip()!r}"
        )
    assert found_match, (
        f"{doc_relpath}: expected at least one `whilly worker connect http://...` example "
        "(this is the M1 quickstart doc; the example must remain so the test continues to "
        "guard against regressions). If you intentionally removed it, update or delete this "
        "parametrized case."
    )
