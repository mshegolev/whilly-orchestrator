"""Pin the Python 3.12+ requirement callout in the three install-facing docs.

`pip install whilly-orchestrator==4.4.0` (and every later release)
fails on Python 3.10 / 3.11 with ``Could not find a version that
satisfies the requirement``. The fix for users is to either run
``python3.12 -m pip install whilly-orchestrator`` or ``pyenv install
3.12 && pyenv local 3.12`` before installing — and the user-facing
docs have to surface that loud and early so users discover it
*before* hitting the cryptic pip error.

This test asserts that the literal token ``python3.12`` appears in
each of the three install-facing docs (README.md,
docs/Distributed-Setup.md, docs/Getting-Started.md), close to the
section that walks the install command. "Close to" is enforced as a
distance metric: the ``python3.12`` token must appear within the
first ``_NEAR_INSTALL_BYTES`` bytes after the first ``pip install``
or ``pipx install`` mention in the file (the install entry-point a
copy-paste-er sees), so it cannot be buried as a footnote at the
bottom.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_DOCS_WITH_CALLOUT: tuple[str, ...] = (
    "README.md",
    "docs/Distributed-Setup.md",
    "docs/Getting-Started.md",
)

_NEAR_INSTALL_BYTES: int = 600


@pytest.mark.parametrize("doc_relpath", _DOCS_WITH_CALLOUT)
def test_doc_exists(doc_relpath: str) -> None:
    path = REPO_ROOT / doc_relpath
    assert path.is_file(), f"expected install-facing doc at {doc_relpath}"


@pytest.mark.parametrize("doc_relpath", _DOCS_WITH_CALLOUT)
def test_python_312_token_present(doc_relpath: str) -> None:
    text = (REPO_ROOT / doc_relpath).read_text(encoding="utf-8")
    assert "python3.12" in text or "3.12" in text, (
        f"{doc_relpath}: expected literal '3.12' or 'python3.12' to surface the Python 3.12+ install requirement"
    )


@pytest.mark.parametrize("doc_relpath", _DOCS_WITH_CALLOUT)
def test_python_312_callout_is_near_install_instructions(doc_relpath: str) -> None:
    text = (REPO_ROOT / doc_relpath).read_text(encoding="utf-8")
    install_anchors: list[int] = []
    for needle in ("pip install", "pipx install"):
        idx = text.find(needle)
        if idx >= 0:
            install_anchors.append(idx)
    assert install_anchors, (
        f"{doc_relpath}: no `pip install` / `pipx install` instruction found — "
        "this test expects the callout to live near install instructions"
    )
    earliest_install = min(install_anchors)

    window_start = max(0, earliest_install - _NEAR_INSTALL_BYTES)
    window_end = earliest_install + _NEAR_INSTALL_BYTES
    window = text[window_start:window_end]
    assert "python3.12" in window or "3.12" in window, (
        f"{doc_relpath}: expected '3.12' / 'python3.12' to appear within "
        f"{_NEAR_INSTALL_BYTES} bytes of the first install command "
        f"(at byte {earliest_install}); callout looks buried far from "
        "the install section"
    )
