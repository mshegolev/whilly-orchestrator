"""End-of-mission v4.6.1 README pass — pin the headline release coordinates.

The final-pass mission feature ``final-readme-pass-v4.6.1`` rewrites the
README to reflect the final v5.0 mission state (HTMX dashboard + SSE +
Prometheus + extended health triplet + ``localhost.run`` funnel sidecar
+ Python 3.12+ install pin). The headline release coordinates a
copy-paste-er needs to upgrade are the literal version string ``4.6.1``
near the install instructions and the literal multi-arch Docker tag
``mshegolev/whilly:4.6.1`` in a ``docker pull`` example.

This test pins three invariants on ``README.md``:

1. ``4.6.1`` appears at least once **near the install instructions**
   — within ``_NEAR_INSTALL_BYTES`` of the first ``pip install`` /
   ``pipx install`` mention. This protects the install callout from
   silently falling out of sync with the published PyPI release.
2. The literal token ``mshegolev/whilly:4.6.1`` appears at least once
   in a ``docker pull`` line — proving the README documents the
   correct multi-arch Docker Hub tag a user would pull.
3. No stale ``mshegolev/whilly:4.4.x`` / ``4.5.x`` / ``4.6.0`` image
   references survive — old tags must not silently linger after the
   release bump.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

_NEAR_INSTALL_BYTES: int = 1200

_STALE_IMAGE_PATTERNS: tuple[str, ...] = (
    r"mshegolev/whilly:4\.4\.\d+",
    r"mshegolev/whilly:4\.5\.\d+",
    r"mshegolev/whilly:4\.6\.0\b",
)


def test_readme_exists() -> None:
    assert README.is_file(), "expected README.md at repository root"


def test_v461_version_present_near_install_instructions() -> None:
    """``4.6.1`` MUST appear within ``_NEAR_INSTALL_BYTES`` of the first
    ``pip install`` / ``pipx install`` anchor in README.md.
    """
    text = README.read_text(encoding="utf-8")
    install_anchors: list[int] = []
    for needle in ("pip install", "pipx install"):
        idx = text.find(needle)
        if idx >= 0:
            install_anchors.append(idx)
    assert install_anchors, (
        "README.md: no `pip install` / `pipx install` instruction found — "
        "this test expects the v4.6.1 callout to live near install instructions"
    )
    earliest_install = min(install_anchors)

    window_start = max(0, earliest_install - _NEAR_INSTALL_BYTES)
    window_end = earliest_install + _NEAR_INSTALL_BYTES
    window = text[window_start:window_end]
    assert "4.6.1" in window, (
        f"README.md: expected literal '4.6.1' to appear within "
        f"{_NEAR_INSTALL_BYTES} bytes of the first install command "
        f"(at byte {earliest_install}); v4.6.1 release coordinate "
        "looks buried far from the install section"
    )


def test_docker_pull_example_uses_4_6_1_tag() -> None:
    """A literal ``docker pull mshegolev/whilly:4.6.1`` must be documented.

    The README must explicitly reference the multi-arch
    ``mshegolev/whilly:4.6.1`` Docker Hub tag in a ``docker pull``
    line so a copy-paste-er sees the correct tag.
    """
    text = README.read_text(encoding="utf-8")
    assert "mshegolev/whilly:4.6.1" in text, (
        "README.md: expected literal token 'mshegolev/whilly:4.6.1' "
        "(the multi-arch Docker Hub tag for the v4.6.1 release)"
    )

    docker_pull_re = re.compile(r"docker\s+pull\s+mshegolev/whilly:4\.6\.1")
    assert docker_pull_re.search(text), (
        "README.md: expected a literal `docker pull mshegolev/whilly:4.6.1` "
        "example so a copy-paste-er can grab the multi-arch image directly"
    )


def test_no_stale_legacy_image_tag_references() -> None:
    """No ``mshegolev/whilly:4.4.x`` / ``4.5.x`` / ``4.6.0`` references survive.

    Old image tags must not linger in the README after the v4.6.1
    bump — they would mislead users into pulling a stale image.
    """
    text = README.read_text(encoding="utf-8")
    failures: list[str] = []
    for pattern in _STALE_IMAGE_PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            failures.append(f"pattern {pattern!r} matched: {matches}")
    assert not failures, "README.md contains stale image-tag references:\n  " + "\n  ".join(failures)
