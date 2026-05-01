"""Doc-string regression test for M1 user-testing round-1 findings.

Bundles four small content assertions that map 1:1 to validation-contract
items the orchestrator flagged as blocking after M1 round-1:

* ``VAL-M1-DOCS-001``: ``docs/Distributed-Setup.md`` MUST contain the literal
  modern Docker Compose v2 invocation
  ``docker compose -f docker-compose.control-plane.yml up -d`` (note the
  *space* between ``docker`` and ``compose`` — Compose v2 dropped the
  hyphen). The doc may continue to ship the legacy ``docker-compose``
  (dash) form alongside; this test only asserts the v2 form is present.
* ``VAL-CROSS-UX-901``: ``README-RU.md`` MUST contain at least one link to
  ``docs/Distributed-Setup.md`` so a Russian-speaking operator can
  discover the multi-host walkthrough from the localized README.
* ``VAL-CROSS-WSDOC-903``: ``docs/Workspace-Topology.md`` Option A MUST
  include a copy-paste-runnable worked example with the literal
  ``git clone --branch worker-A`` / ``git push origin worker-A/...``
  commands inside a fenced code block.
* ``VAL-CROSS-BACKCOMPAT-908``: ``library/baselines/whilly-report-template.md``
  MUST exist and define the canonical headings (``# Whilly Cost Report``,
  ``## Summary``, ``## Plans``) future Reporter outputs are diffed
  against.

These are intentionally lightweight ``rg``-style content checks; the
behavioral contract is "the literal string is in the right file", not
"the surrounding prose makes sense" — that's covered by other docs
tests.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.is_file(), f"missing required file: {rel}"
    return path.read_text(encoding="utf-8")


def test_distributed_setup_has_docker_compose_v2_form() -> None:
    """VAL-M1-DOCS-001: doc contains modern ``docker compose`` (space) form."""
    body = _read("docs/Distributed-Setup.md")
    needle = "docker compose -f docker-compose.control-plane.yml up -d"
    assert needle in body, (
        "docs/Distributed-Setup.md must contain the modern Docker Compose v2 "
        f"invocation literally: {needle!r}. Found neither space nor dash form "
        "matching the expected control-plane bring-up command."
    )


def test_readme_ru_links_distributed_setup() -> None:
    """VAL-CROSS-UX-901: localized README references the distributed setup doc."""
    body = _read("README-RU.md")
    needle = "docs/Distributed-Setup.md"
    assert needle in body, (
        "README-RU.md must link to docs/Distributed-Setup.md so RU readers can discover the multi-host walkthrough."
    )


def test_workspace_topology_has_worker_a_worked_example() -> None:
    """VAL-CROSS-WSDOC-903: Option A worked example with the canonical commands."""
    body = _read("docs/Workspace-Topology.md")
    clone_needle = "git clone --branch worker-A"
    push_needle = "git push origin worker-A/"
    assert clone_needle in body, (
        f"docs/Workspace-Topology.md must include the literal {clone_needle!r} command inside a worked example."
    )
    assert push_needle in body, (
        f"docs/Workspace-Topology.md must include the literal {push_needle!r} command inside a worked example."
    )


def test_whilly_report_template_baseline_exists() -> None:
    """VAL-CROSS-BACKCOMPAT-908: canonical structural baseline for Reporter output."""
    rel = "library/baselines/whilly-report-template.md"
    body = _read(rel)
    # The baseline is a reduced-form template; we assert the canonical
    # headings the Reporter emits via ``generate_summary``. If those
    # change in the Reporter, both the Reporter and this baseline must
    # be updated together (the test is intentionally tight).
    expected_headings = [
        "# Whilly Cost Report",
        "## Summary",
        "## Plans",
    ]
    for heading in expected_headings:
        assert heading in body, (
            f"{rel} must contain heading {heading!r} so external dashboards "
            "diffing against this baseline don't break when Reporter output "
            "regresses."
        )
