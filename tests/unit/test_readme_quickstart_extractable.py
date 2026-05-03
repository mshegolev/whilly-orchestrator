"""README.md + docs/Distributed-Setup.md quickstart bash blocks must be extractable.

The user-testing validator (round 5: ``VAL-CROSS-UX-902`` and
``VAL-M1-DOCS-002``) extracts every fenced ``bash`` block from these two
quickstart surfaces and pipes the contents into ``bash`` to prove a
copy-paste-er can run the documented commands without manual edits.

The previous quickstart blocks failed extraction because:

1. They embedded literal placeholders such as ``path/to/tasks.json``,
   ``<vps-ip>``, ``control.example.com``, and ``vps.example.com`` that
   ``bash`` cannot run as-is.
2. They interleaved long-running server commands (``uvicorn``,
   ``docker compose logs -f``) with one-shot setup commands inside the
   same fenced block, so the extracted script never returns to the
   shell.

This test pins three invariants for every fenced ``bash`` block in
``README.md`` and ``docs/Distributed-Setup.md``:

(a) The block parses cleanly under ``bash -n`` (POSIX syntax check).
(b) Every ``$VAR`` reference in the block has a matching definition
    (``export VAR=...`` or plain ``VAR=...``) earlier in the same
    file — either in the same block or in an earlier ``bash`` block —
    so a reader who pastes a single block (or the whole file's blocks
    in order) doesn't hit an undefined-variable expansion.
(c) No literal ``<placeholder>`` or ``path/to/`` patterns appear
    inside any non-comment line of any bash block.

A small allowlist of well-known shell-managed variables is exempted
from invariant (b) because they're set by the user's login shell, not
by the documented snippet.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

QUICKSTART_DOCS: tuple[str, ...] = (
    "README.md",
    "docs/Distributed-Setup.md",
)

_BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)

_VAR_USE_RE = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\b")
_VAR_DEF_RE = re.compile(r"(?m)^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=")

_SHELL_PROVIDED_VARS: frozenset[str] = frozenset(
    {
        "EDITOR",
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "PWD",
        "OLDPWD",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "HOSTNAME",
        "DISPLAY",
        "PS1",
        "IFS",
    }
)

_PLACEHOLDER_RE = re.compile(r"<[a-z][a-z0-9_-]*>")
_PATH_TO_LITERAL = "path/to/"


def _extract_bash_blocks(text: str) -> list[tuple[int, str]]:
    """Return ``[(start_line_1based, body), ...]`` for every fenced bash block.

    Line number is the line of the opening ````` ``bash ````` fence in
    ``text`` — useful for human-readable failure messages.
    """
    blocks: list[tuple[int, str]] = []
    for match in _BASH_BLOCK_RE.finditer(text):
        prefix = text[: match.start()]
        start_line = prefix.count("\n") + 1
        blocks.append((start_line, match.group(1)))
    return blocks


def _vars_used(body: str) -> set[str]:
    return set(_VAR_USE_RE.findall(body))


def _vars_defined(body: str) -> set[str]:
    return set(_VAR_DEF_RE.findall(body))


def _strip_comments(line: str) -> str:
    """Return ``line`` with any trailing ``#`` comment removed.

    We only strip when ``#`` appears OUTSIDE single/double quotes —
    otherwise we'd false-positive on URL fragments or printf-style
    comment-like literals. The bash blocks in the quickstart docs are
    simple enough that a quote-state walk suffices; we don't need full
    bash tokenisation.
    """
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:idx]
    return line


def _placeholder_violations(body: str) -> list[tuple[int, str, str]]:
    """Return ``(line_no, line, kind)`` for each placeholder/path-to violation
    found in a non-comment portion of any line in ``body``.

    Comment-only lines are exempt — explanatory comments may legitimately
    reference ``<placeholder>`` syntax (e.g. ``# replace <slug> with your id``).
    """
    violations: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        executable = _strip_comments(line)
        for match in _PLACEHOLDER_RE.finditer(executable):
            violations.append((line_no, line, match.group(0)))
        if _PATH_TO_LITERAL in executable:
            violations.append((line_no, line, _PATH_TO_LITERAL))
    return violations


@pytest.mark.parametrize("doc_relpath", QUICKSTART_DOCS)
def test_quickstart_doc_exists(doc_relpath: str) -> None:
    path = REPO_ROOT / doc_relpath
    assert path.is_file(), f"expected quickstart doc at {doc_relpath}"


@pytest.mark.parametrize("doc_relpath", QUICKSTART_DOCS)
def test_each_bash_block_parses_with_bash_n(doc_relpath: str) -> None:
    """Every fenced ``bash`` block must parse under ``bash -n``.

    A naive extractor pipes the block body verbatim into ``bash``;
    POSIX syntax errors here mean a copy-paste-er hits a parse error
    before the first command runs.
    """
    path = REPO_ROOT / doc_relpath
    text = path.read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(text)
    assert blocks, f"{doc_relpath}: no fenced ```bash blocks found"
    failures: list[str] = []
    for start_line, body in blocks:
        proc = subprocess.run(
            ["bash", "-n"],
            input=body,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            failures.append(
                f"{doc_relpath}:{start_line}: bash -n exit {proc.returncode}\n"
                f"stderr: {proc.stderr.strip()}\nbody:\n{body}"
            )
    assert not failures, "fenced bash block(s) failed bash -n:\n\n" + "\n\n".join(failures)


@pytest.mark.parametrize("doc_relpath", QUICKSTART_DOCS)
def test_every_var_use_has_earlier_definition(doc_relpath: str) -> None:
    """Every ``$VAR`` use must be matched by an earlier definition in the same file.

    Definition = ``export VAR=...`` or plain ``VAR=...`` (e.g. the
    documented ``WORKER_TOKEN=$(...)`` capture pattern). The match is
    file-scoped (across all bash blocks in document order), so a later
    block can use a variable defined in an earlier block — that's how
    the quickstart's section-3 ``export`` lines feed into section-4's
    ``--connect "$WHILLY_CONTROL_URL"`` invocation.

    The :data:`_SHELL_PROVIDED_VARS` allowlist exempts a small set of
    well-known login-shell-provided variables (``$EDITOR``, ``$HOME``,
    etc.) that are not the snippet's responsibility to set.
    """
    path = REPO_ROOT / doc_relpath
    text = path.read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(text)
    assert blocks, f"{doc_relpath}: no fenced ```bash blocks found"

    defined_so_far: set[str] = set()
    failures: list[str] = []
    for start_line, body in blocks:
        used = _vars_used(body)
        block_defs = _vars_defined(body)
        for var in sorted(used):
            if var in _SHELL_PROVIDED_VARS:
                continue
            if var in defined_so_far or var in block_defs:
                continue
            failures.append(
                f"{doc_relpath} (block at line {start_line}): ${var} used without earlier export/assignment"
            )
        defined_so_far |= block_defs

    assert not failures, (
        "found `$VAR` references with no earlier `export VAR=...` / `VAR=...` "
        "definition in the same file:\n  " + "\n  ".join(failures)
    )


@pytest.mark.parametrize("doc_relpath", QUICKSTART_DOCS)
def test_no_placeholders_or_path_to_inside_bash_blocks(doc_relpath: str) -> None:
    """No ``<placeholder>`` or ``path/to/`` literal may appear in any
    non-comment line of any fenced ``bash`` block.

    Comment-only lines are exempt — explanatory ``# replace <foo>`` is
    fine because the extractor will never execute it.
    """
    path = REPO_ROOT / doc_relpath
    text = path.read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(text)
    assert blocks, f"{doc_relpath}: no fenced ```bash blocks found"
    failures: list[str] = []
    for start_line, body in blocks:
        for line_no, line, kind in _placeholder_violations(body):
            failures.append(f"{doc_relpath}:{start_line + line_no}: {kind!r} in non-comment line: {line.rstrip()!r}")
    assert not failures, (
        "fenced bash block contained literal placeholder / path-to pattern in a "
        "non-comment line — extractor will fail:\n  " + "\n  ".join(failures)
    )


def test_long_running_block_is_segregated_in_readme() -> None:
    """The README quickstart MUST split long-running server commands
    (``uvicorn``) into a separate bash block headed with the literal
    comment ``# Run in a second terminal — long-running:`` so a naive
    extractor can drop it.
    """
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(text)
    second_terminal_blocks = [body for _, body in blocks if "# Run in a second terminal — long-running:" in body]
    assert second_terminal_blocks, (
        "README.md has no fenced bash block carrying the literal `# Run in a "
        "second terminal — long-running:` header; long-running commands like "
        "uvicorn must live in such a block to avoid blocking the extractor."
    )
    long_running_block = "\n".join(second_terminal_blocks)
    assert "uvicorn" in long_running_block, (
        "the second-terminal long-running block must contain the documented uvicorn invocation"
    )

    main_blocks = [body for _, body in blocks if "# Run in a second terminal — long-running:" not in body]
    main_text = "\n".join(main_blocks)
    assert "uvicorn" not in main_text, (
        "uvicorn must NOT appear in any quickstart bash block other than the second-terminal long-running block"
    )
    assert (
        "docker compose logs -f" not in main_text
        and "docker-compose -f docker-compose.control-plane.yml logs -f" not in main_text
    ), (
        "docker compose logs -f must NOT appear in any quickstart bash block other "
        "than the second-terminal long-running block"
    )


def test_helpers_strip_comments_correctly() -> None:
    """Direct unit test for :func:`_strip_comments` so the placeholder
    test's gating logic is itself covered.
    """
    assert _strip_comments("foo bar # comment") == "foo bar "
    assert _strip_comments("echo 'hi # not comment'") == "echo 'hi # not comment'"
    assert _strip_comments('echo "hi # not comment"') == 'echo "hi # not comment"'
    assert _strip_comments("# whole line") == ""
    assert _strip_comments("no comment here") == "no comment here"


def test_helpers_extract_blocks_from_synthetic_doc() -> None:
    text = "intro\n```bash\nfoo\nbar\n```\nmid\n```bash\nbaz\n```\n"
    blocks = _extract_bash_blocks(text)
    assert len(blocks) == 2
    assert blocks[0][1] == "foo\nbar"
    assert blocks[1][1] == "baz"


def test_helpers_var_use_and_def_extraction() -> None:
    body = "export FOO=1\nbar=$(echo hi)\necho $FOO ${BAR} $bar\n"
    assert _vars_used(body) == {"FOO", "BAR"}
    assert _vars_defined(body) == {"FOO"}
