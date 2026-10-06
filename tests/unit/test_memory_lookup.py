from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import asyncio

import pytest

from whilly.adapters.filesystem.memory_leases import LeaseStore
from whilly.swarm.learning.domain import KnowledgeRevision, Principal
from whilly.swarm.learning.lookup import LookupService
from whilly.swarm.learning.retrieval import RetrievalRecord, RetrievalRequest, RetrievalResult, RetrievalUnavailable


NOW = datetime.now(timezone.utc)


def _revision(revision_id: str, *, product: str = "p", project: str = "x", body: str = "body") -> KnowledgeRevision:
    return KnowledgeRevision(
        id=revision_id,
        product_id=product,
        project_id=project,
        kind="fact",
        body=body,
        source_uri="git:file",
        source_sha="a" * 40,
        evidence_hash="e",
        observed_at=NOW,
        verified_at=NOW,
        expires_at=None,
        classification="internal",
        status="verified",
        author_id="a",
        verifier_id="v",
        policy_version="1",
    )


class Store:
    def __init__(self, items):
        self.items = items

    async def visible(self, principal, product_id, project_ids):
        return list(self.items)


class Lease:
    directory = None
    lock_fds = ()
    id = "lease"

    def cancelled(self):
        return False

    async def register(self, ids):
        self.ids = ids


class Leases:
    def reserve(self, deadline):
        class Ctx:
            async def __aenter__(self):
                return Lease()

            async def __aexit__(self, *args):
                return False

        return Ctx()

    def fence(self):
        class Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *args):
                return False

        return Ctx()


def test_retrieval_contract_exposes_lock_fds_and_dtos() -> None:
    request = RetrievalRequest(query="q", records=(RetrievalRecord("r1", "body"),))
    result = RetrievalResult(ids=("r1",), elapsed_ms=1)
    assert request.records[0].id == "r1"
    assert result.ids == ("r1",)
    assert LookupService is not None


@pytest.mark.asyncio
async def test_acl_and_verifier_filter_before_retriever_body_copy() -> None:
    seen = []

    class Retriever:
        async def rank(self, request, **kwargs):
            seen.extend(request.records)
            return RetrievalResult(("good",), 1)

    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified" if item.id == "good" else "unavailable", item.source_sha)

    service = LookupService(Store([_revision("good"), _revision("bad")]), Retriever(), Leases(), Verifier())
    package = await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)
    assert [record.id for record in seen] == ["good"]
    assert package.items[0].id == "good"


@pytest.mark.asyncio
async def test_duplicate_rank_ids_are_rejected() -> None:
    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified", item.source_sha)

    class Retriever:
        async def rank(self, request, **kwargs):
            return RetrievalResult(("good", "good"), 1)

    service = LookupService(Store([_revision("good")]), Retriever(), Leases(), Verifier())
    with pytest.raises(RetrievalUnavailable, match="duplicate"):
        await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)


@pytest.mark.asyncio
async def test_missing_verifier_is_fail_closed() -> None:
    class Retriever:
        async def rank(self, request, **kwargs):
            raise AssertionError("backend must not start")

    service = LookupService(Store([_revision("good")]), Retriever(), Leases())
    package = await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)
    assert package.items == ()


@pytest.mark.asyncio
async def test_timeout_cancels_and_awaits_rank_task(tmp_path: Path) -> None:
    started = asyncio.Barrier(2)
    cancelled = asyncio.Event()

    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified", item.source_sha)

    class Retriever:
        async def rank(self, request, **kwargs):
            await started.wait()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    service = LookupService(Store([_revision("good")]), Retriever(), LeaseStore(tmp_path), Verifier(), timeout=0.03)
    task = asyncio.create_task(
        service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)
    )
    await started.wait()
    with pytest.raises(RetrievalUnavailable, match="timeout|cancel"):
        await task
    assert cancelled.is_set()
    assert not list(tmp_path.glob("*/manifest.json"))


@pytest.mark.asyncio
async def test_input_is_deterministically_bounded_and_unranked_are_omissions() -> None:
    seen = []

    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified", item.source_sha)

    class Retriever:
        async def rank(self, request, **kwargs):
            seen.append(request)
            return RetrievalResult((), 1)

    records = [_revision(f"r-{index}", body="x" * 6000) for index in range(40)]
    service = LookupService(Store(records), Retriever(), Leases(), Verifier())
    package = await service.context(
        Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q" * 4096, max_chars=1000
    )
    assert len(seen) == 1
    assert len(seen[0].query) == 4096
    assert len(seen[0].records) <= 32
    assert all(len(record.body) == 6000 for record in seen[0].records)
    assert {item.id for item in records} <= set(package.omissions)


@pytest.mark.asyncio
async def test_redaction_cancels_lookup_before_it_can_return(tmp_path: Path) -> None:
    entered = asyncio.Barrier(2)
    released = asyncio.Event()

    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified", item.source_sha)

    class MutableStore(Store):
        async def redact(self, principal, revision_id):
            self.items = []
            return True

    class Retriever:
        async def rank(self, request, **kwargs):
            await entered.wait()
            while not released.is_set():
                await asyncio.sleep(0.01)
            return RetrievalResult((), 1)

    store = MutableStore([_revision("good")])
    service = LookupService(store, Retriever(), LeaseStore(tmp_path), Verifier(), timeout=1)
    task = asyncio.create_task(
        service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)
    )
    await asyncio.wait_for(entered.wait(), 1)
    redaction = await service.redact(Principal("a", ("p",), ("x",), ("internal",)), "good")
    released.set()
    with pytest.raises(Exception, match="cancel"):
        await task
    assert redaction.redacted


@pytest.mark.asyncio
async def test_foreign_id_is_rejected() -> None:
    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified", item.source_sha)

    class Retriever:
        async def rank(self, request, **kwargs):
            return RetrievalResult(("not-registered",), 1)

    service = LookupService(Store([_revision("good")]), Retriever(), Leases(), Verifier())
    with pytest.raises(RetrievalUnavailable, match="unknown"):
        await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)


@pytest.mark.asyncio
async def test_canonical_reread_rejects_changed_revision() -> None:
    class Verifier:
        async def check(self, item):
            from whilly.swarm.learning.domain import SourceCheck

            return SourceCheck("verified", item.source_sha)

    class ChangingStore(Store):
        calls = 0

        async def visible(self, principal, product_id, project_ids):
            self.calls += 1
            return [_revision("good", body="changed")] if self.calls >= 4 else list(self.items)

    class Retriever:
        async def rank(self, request, **kwargs):
            return RetrievalResult(("good",), 1)

    service = LookupService(ChangingStore([_revision("good")]), Retriever(), Leases(), Verifier())
    with pytest.raises(RetrievalUnavailable, match="canonical_revision_changed"):
        await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)


@pytest.mark.asyncio
async def test_query_limit_is_named_and_backend_is_not_started() -> None:
    class Retriever:
        async def rank(self, request, **kwargs):
            raise AssertionError("query must be rejected before indexing")

    service = LookupService(Store([_revision("good")]), Retriever(), Leases())
    with pytest.raises(RetrievalUnavailable, match="query_limit"):
        await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q" * 4097, max_chars=1000)


@pytest.mark.asyncio
async def test_source_becoming_unavailable_at_final_read_is_rejected() -> None:
    from whilly.swarm.learning.domain import SourceCheck

    class Verifier:
        available = True

        async def check(self, item):
            return SourceCheck("verified" if self.available else "unavailable", item.source_sha)

    verifier = Verifier()

    class FinalReadStore(Store):
        calls = 0

        async def visible(self, principal, product_id, project_ids):
            self.calls += 1
            if self.calls == 4:
                verifier.available = False
            return list(self.items)

    class Retriever:
        async def rank(self, request, **kwargs):
            return RetrievalResult(("good",), 1)

    service = LookupService(FinalReadStore([_revision("good")]), Retriever(), Leases(), verifier)
    with pytest.raises(RetrievalUnavailable, match="canonical_revision_unavailable"):
        await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)


@pytest.mark.asyncio
async def test_tombstone_before_rank_prevents_retriever_invocation() -> None:
    class CancelledLease(Lease):
        def cancelled(self):
            return True

    class Retriever:
        called = False

        async def rank(self, request, **kwargs):
            self.called = True
            return RetrievalResult((), 1)

    retriever = Retriever()
    service = LookupService(Store([]), retriever, Leases())
    with pytest.raises(RetrievalUnavailable, match="redaction_cancelled"):
        await service._rank(CancelledLease(), RetrievalRequest("q", ()), __import__("time").monotonic() + 1)
    assert not retriever.called


@pytest.mark.asyncio
async def test_launch_guard_rechecks_tombstone_before_backend_spawn(tmp_path: Path) -> None:
    from whilly.swarm.learning.domain import SourceCheck

    before_spawn = asyncio.Event()
    continue_spawn = asyncio.Event()
    spawned = []

    class Verifier:
        async def check(self, item):
            return SourceCheck("verified", item.source_sha)

    class Retriever:
        async def rank(self, request, *, launch_guard, **kwargs):
            before_spawn.set()
            await continue_spawn.wait()
            async with launch_guard():
                spawned.append(True)
            return RetrievalResult(("good",), 1)

    leases = LeaseStore(tmp_path)
    service = LookupService(Store([_revision("good")]), Retriever(), leases, Verifier())
    task = asyncio.create_task(
        service.context(
            Principal("a", ("p",), ("x",), ("internal",)),
            "p",
            ("x",),
            "q",
            max_chars=1000,
        )
    )
    await asyncio.wait_for(before_spawn.wait(), 1)
    async with leases.fence():
        affected = leases._cancel_revision_locked("good")
        assert affected
    continue_spawn.set()
    with pytest.raises(RetrievalUnavailable, match="redaction_cancelled"):
        await task
    assert not spawned
    assert not await leases.pending(affected)


@pytest.mark.asyncio
async def test_queue_full_is_named_at_lookup_boundary(tmp_path: Path) -> None:
    from whilly.swarm.learning.domain import SourceCheck

    class Verifier:
        async def check(self, item):
            return SourceCheck("verified", item.source_sha)

    class Retriever:
        async def rank(self, *args, **kwargs):
            raise AssertionError("full queue must not launch backend")

    service = LookupService(Store([_revision("good")]), Retriever(), LeaseStore(tmp_path, slots=0), Verifier())
    with pytest.raises(RetrievalUnavailable, match="queue_full"):
        await service.context(Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q", max_chars=1000)
