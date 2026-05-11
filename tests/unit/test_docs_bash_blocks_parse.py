"""Every fenced ``bash`` block under repo root + docs/ must parse with ``bash -n``.

A copy-paste-er hitting one of our docs and pasting a fenced ``bash``
block into a shell should never see a syntax error before the first
real command runs. The previous failure mode was angle-bracket
placeholders such as ``whilly --post-merge <plan.json>`` — bash treats
``<`` / ``>`` as redirection operators, so ``bash -n`` fails on the
parse before it can even consider executing anything.

The test walks ``*.md`` files at repo root and under ``docs/``,
extracts every triple-backticked ``bash`` block whose opening fence
sits at column 0 (top-level fenced blocks; indented blocks inside
list items are skipped — they're rendered as part of the surrounding
prose, and the closing-fence indentation rules make them brittle to
extract reliably), and pipes each block body into ``bash -n``.

Convention used by this repo to keep these blocks runnable: replace
``<placeholder>`` with ``$PLACEHOLDER`` and prepend an
``export PLACEHOLDER=...`` line at the top of the block (or in an
earlier block in the same file).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_BASH_BLOCK_RE = re.compile(r"(?m)^```bash\n(.*?)\n^```\s*$", re.DOTALL)


def _collect_md_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.glob("*.md"):
        if path.is_file():
            files.append(path)
    docs = REPO_ROOT / "docs"
    if docs.is_dir():
        for path in docs.rglob("*.md"):
            if path.is_file():
                files.append(path)
    return sorted(files)


def _extract_bash_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for match in _BASH_BLOCK_RE.finditer(text):
        prefix = text[: match.start()]
        start_line = prefix.count("\n") + 1
        blocks.append((start_line, match.group(1)))
    return blocks


_MD_FILES = _collect_md_files()


def test_md_files_discovered() -> None:
    assert _MD_FILES, "expected at least one *.md file at repo root + docs/"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize("md_path", _MD_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_each_bash_block_parses_with_bash_n(md_path: Path) -> None:
    text = md_path.read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(text)
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
            rel = md_path.relative_to(REPO_ROOT)
            failures.append(
                f"{rel}:{start_line}: bash -n exit {proc.returncode}\nstderr: {proc.stderr.strip()}\nbody:\n{body}"
            )
    assert not failures, "fenced bash block(s) failed bash -n:\n\n" + "\n\n".join(failures)


def test_extractor_finds_top_level_blocks_only() -> None:
    text = "intro\n```bash\nfoo\n```\nlist:\n  - item:\n    ```bash\n    indented\n    ```\n```bash\nbar\n```\n"
    blocks = _extract_bash_blocks(text)
    bodies = [body for _, body in blocks]
    assert "foo" in bodies
    assert "bar" in bodies
    assert "    indented" not in bodies
