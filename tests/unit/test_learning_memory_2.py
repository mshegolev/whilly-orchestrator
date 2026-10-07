from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from whilly.swarm.learning.domain import ContextPackage, KnowledgeRevision, Principal, SourceCheck
from whilly.swarm.learning.memory import MemoryService, build_context, render_context
from whilly.adapters.filesystem.knowledge_sources import GitSourceVerifier


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def revision(
    identifier: str,
    *,
    status: str = "verified",
    source_uri: str = "git:docs/fact.md",
    source_sha: str | None = "abc123",
    verified_at: datetime | None = NOW,
    expires_at: datetime | None = None,
    kind: str = "fact",
    body: str | None = None,
    conflicts: tuple[str, ...] = (),
) -> KnowledgeRevision:
    return KnowledgeRevision(
        id=identifier,
        product_id="product",
        project_id="project",
        kind=kind,
        body=body or identifier,
        source_uri=source_uri,
        source_sha=source_sha,
        evidence_hash=f"evidence-{identifier}",
        observed_at=NOW,
        verified_at=verified_at,
        expires_at=expires_at,
        classification="internal",
        status=status,
        author_id="author",
        verifier_id="verifier",
        policy_version="v1",
        conflicts=conflicts,
    )


def test_git_verifier_accepts_file_at_exact_base_revision(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "docs"
    source.mkdir()
    (source / "fact.md").write_text("fact\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=test", "-c", "user.email=test@example", "commit", "-qm", "init"],
        check=True,
    )
    sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()

    verifier = GitSourceVerifier({"project": (str(tmp_path), sha)})
    result = __import__("asyncio").run(verifier.check(revision("r1", source_sha=sha)))

    assert result == SourceCheck(status="verified", revision=sha)


@pytest.mark.asyncio
async def test_git_verifier_returns_commit_sha_and_uses_immutable_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = "a" * 40
    calls: list[tuple[str, ...]] = []

    async def fake_git(repo: str, *args: str) -> str:
        calls.append(args)
        if args[:2] == ("rev-parse", "ref^{commit}"):
            return expected
        return "100644 blob 123\tdocs/fact.md"

    monkeypatch.setattr(GitSourceVerifier, "_git", staticmethod(fake_git))
    result = await GitSourceVerifier({"project": (str(tmp_path), "ref")}).check(revision("r1", source_sha=expected))

    assert result == SourceCheck("verified", expected)
    assert calls == [
        ("rev-parse", "ref^{commit}"),
        ("ls-tree", expected, "--", "docs/fact.md"),
        ("cat-file", "-e", f"{expected}:docs/fact.md"),
    ]


@pytest.mark.asyncio
async def test_git_verifier_rejects_directory_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = "a" * 40

    async def fake_git(repo: str, *args: str) -> str:
        if args[0] == "rev-parse":
            return expected
        return "040000 tree 123\tdocs"

    monkeypatch.setattr(GitSourceVerifier, "_git", staticmethod(fake_git))
    result = await GitSourceVerifier({"project": (str(tmp_path), "ref")}).check(revision("r1", source_sha=expected))

    assert result == SourceCheck("unavailable", None)


@pytest.mark.asyncio
async def test_git_subprocess_is_killed_and_reaped_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingProcess:
        returncode = None
        killed = False
        reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            raise asyncio.TimeoutError

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> None:
            self.reaped = True

    import asyncio

    process = HangingProcess()

    async def create(*args: str, **kwargs: object) -> HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(asyncio.TimeoutError):
        await GitSourceVerifier._git("/repo", "rev-parse", "ref")
    assert process.killed is True
    assert process.reaped is True


@pytest.mark.asyncio
async def test_git_verifier_rejects_changed_sha_missing_source_and_escape(tmp_path: Path) -> None:
    verifier = GitSourceVerifier({"project": (str(tmp_path), "expected")})

    changed = await verifier.check(revision("changed", source_sha="different"))
    missing = await verifier.check(revision("missing", source_uri="git:missing.md", source_sha="expected"))
    outside = await verifier.check(revision("outside", source_uri="git:../secret", source_sha="expected"))

    assert changed.status == missing.status == outside.status == "unavailable"


def test_context_is_deterministic_bounded_and_keeps_explicit_metadata() -> None:
    items = [
        revision("candidate", status="candidate", kind="hypothesis", body="candidate body"),
        revision("verified", body="verified body", conflicts=("conflict-id",)),
        revision("expired", expires_at=NOW - timedelta(seconds=1)),
    ]

    package = build_context(items, max_chars=512, now=NOW)
    rendered = render_context(package)

    assert isinstance(package, ContextPackage)
    assert package.revision_manifest == tuple(item.id for item in package.items)
    assert package.items[0].id == "candidate"
    assert "expired" in package.omissions
    assert "conflict-id" in package.conflicts
    assert len(rendered) <= 512
    assert render_context(package) == rendered
    assert "candidate" in rendered


def test_context_rejects_too_small_budget() -> None:
    with pytest.raises(ValueError):
        build_context([], max_chars=255, now=NOW)


def test_context_budget_includes_huge_metadata_and_empty_items() -> None:
    items = [revision(f"expired-{index}", expires_at=NOW - timedelta(seconds=1)) for index in range(100)]

    rendered = render_context(build_context(items, max_chars=256, now=NOW))

    assert len(rendered) <= 256


def test_oversized_metadata_terminates() -> None:
    code = """
from datetime import datetime, timezone
from whilly.swarm.learning.memory import build_context
from whilly.swarm.learning.domain import KnowledgeRevision
now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
items = [KnowledgeRevision(
    id=f'omission-{i}-' + 'x' * 100, product_id='product', project_id='project',
    kind='fact', body='x', source_uri='git:fact.md', source_sha='sha',
    evidence_hash='e', observed_at=now, verified_at=now, expires_at=now,
    classification='internal', status='verified', author_id='a', verifier_id='v', policy_version='v1')
    for i in range(5000)]
build_context(items, max_chars=256, now=now)
"""
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        pytest.fail("metadata bounding did not terminate")
    assert child.returncode == 0


def test_mixed_oversized_metadata_terminates() -> None:
    code = """
from datetime import datetime, timezone
from whilly.swarm.learning.memory import build_context
from whilly.swarm.learning.domain import KnowledgeRevision
now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
items = [KnowledgeRevision(
    id=f'omission-{i}-' + 'x' * 100, product_id='product', project_id='project',
    kind='fact', body='x', source_uri='git:fact.md', source_sha='sha',
    evidence_hash='e', observed_at=now, verified_at=now, expires_at=now,
    classification='internal', status='verified', author_id='a', verifier_id='v',
    policy_version='v1', conflicts=(f'conflict-{i}-' + 'y' * 100,))
    for i in range(5000)]
build_context(items, max_chars=256, now=now)
"""
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        pytest.fail("mixed metadata bounding did not terminate")
    assert child.returncode == 0


def test_late_omissions_fit_budget() -> None:
    package = build_context(
        [revision("kept", body="текст" * 20)],
        max_chars=256,
        now=NOW,
        omissions=("🚫" * 40 + "https://example.test/" + str(index) for index in range(1000)),
    )
    assert len(render_context(package)) <= 256


def test_preserve_order_uses_first_ranked_fitting_record() -> None:
    ranked = [revision("rank-2", body="b" * 90), revision("rank-1", body="a" * 90)]

    preserved = build_context(ranked, max_chars=430, now=NOW, preserve_order=True)
    defaulted = build_context(ranked, max_chars=430, now=NOW)

    assert [item.id for item in preserved.items] == ["rank-2"]
    assert [item.id for item in defaulted.items] == ["rank-1"]


def test_oversized_first_item_is_explicitly_omitted() -> None:
    package = build_context([revision("huge", body="x" * 16000)], max_chars=256, now=NOW)

    assert package.items == ()
    assert "huge" in package.omissions
    assert len(render_context(package)) <= 256


def test_stale_and_conflicted_content_are_omitted_but_metadata_is_retained() -> None:
    package = build_context(
        [revision("stale", status="stale"), revision("conflicted", conflicts=("other",))],
        max_chars=512,
        now=NOW,
    )

    assert package.items == ()
    assert {"stale", "conflicted"}.issubset(package.omissions)
    assert package.conflicts == ("other",)


def test_render_context_uses_revision_manifest_and_serializes_provenance() -> None:
    item = revision("provenance", source_sha="a" * 40, expires_at=NOW + timedelta(days=1))
    data = json.loads(render_context(build_context([item], max_chars=1024, now=NOW)))

    assert "revision_manifest" in data
    assert "manifest" not in data
    assert data["items"][0]["source_uri"] == item.source_uri
    assert data["items"][0]["source_sha"] == item.source_sha
    assert data["items"][0]["verified_at"] == item.verified_at.isoformat()
    assert data["items"][0]["expires_at"] == item.expires_at.isoformat()


class InMemoryStore:
    def __init__(self, items: list[KnowledgeRevision]) -> None:
        self.items = items

    async def visible(
        self, principal: Principal, product_id: str, project_ids: tuple[str, ...]
    ) -> list[KnowledgeRevision]:
        return [item for item in self.items if item.product_id == product_id and item.project_id in project_ids]


@pytest.mark.asyncio
async def test_memory_service_filters_before_context_and_downgrades_unavailable_sources() -> None:
    class UnavailableVerifier:
        async def check(self, item: KnowledgeRevision) -> SourceCheck:
            return SourceCheck(status="unavailable", revision=item.id)

    visible = revision("visible")
    hidden = replace(visible, id="hidden", project_id="other")
    service = MemoryService(InMemoryStore([visible, hidden]), UnavailableVerifier())
    principal = Principal("actor", ("product",), ("project",), ("internal",))

    package = await service.context(principal, "product", ("project",), max_chars=512, now=NOW)

    assert [item.id for item in package.items] == ["visible"]
    assert package.items[0].status == "candidate"
    assert package.items[0].verified_at is None


@pytest.mark.asyncio
async def test_external_source_requires_unexpired_expiry() -> None:
    class ExplodingVerifier:
        async def check(self, item: KnowledgeRevision) -> SourceCheck:
            raise AssertionError("external sources must not be fetched")

    item = revision("external", source_uri="https://example.test/fact", source_sha=None, expires_at=None)
    principal = Principal("actor", ("product",), ("project",), ("internal",))
    service = MemoryService(InMemoryStore([item]), ExplodingVerifier())

    package = await service.context(principal, "product", ("project",), max_chars=512, now=NOW)

    assert package.items == ()
    assert "external" in package.omissions


@pytest.mark.asyncio
async def test_memory_service_bounds_late_external_omissions() -> None:
    items = [
        revision(f"external-{index}", source_uri=f"https://example.test/{index}", source_sha=None, expires_at=None)
        for index in range(1000)
    ]
    principal = Principal("actor", ("product",), ("project",), ("internal",))
    service = MemoryService(InMemoryStore(items), object())

    package = await service.context(principal, "product", ("project",), max_chars=256, now=NOW)

    rendered = render_context(package)
    assert len(rendered) <= 256
    assert package.omissions == ("bounded:1000 omissions",)
