"""Coordinator binds proposal origin and submit-only scope, not worker JSON."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from whilly.swarm.runtime import Coordinator


@pytest.mark.asyncio
async def test_product_coordinator_pins_proposal_mailbox_identity(monkeypatch, tmp_path):
    from whilly.adapters.db import learning_delivery, learning_proposals
    from whilly.swarm import mailbox, runtime

    sync = AsyncMock()
    monkeypatch.setitem(sys.modules, "whilly.swarm.proposal_mailbox", SimpleNamespace(sync_proposal_mailbox=sync))
    monkeypatch.setattr(runtime, "sync_mailbox", AsyncMock())
    monkeypatch.setattr(mailbox, "sync_collaboration_mailbox", AsyncMock())
    monkeypatch.setattr(learning_delivery, "delivery_policy_from_env", lambda: None)
    monkeypatch.setattr(learning_delivery, "build_delivery_service", lambda *a, **kw: None)
    monkeypatch.setattr(learning_proposals, "PostgresProposalStore", lambda *a, **kw: object())
    coordinator = SimpleNamespace(
        store=object(),
        session_id="session-1",
        service=SimpleNamespace(pool=object()),
        _collaboration_feature={"id": "feature-1", "product_id": "product-1"},
        registry=SimpleNamespace(
            projects={"demo": object()}, roles={"implementer": SimpleNamespace(projects=("demo",))}
        ),
    )
    await Coordinator._sync_task_mailbox(
        coordinator, tmp_path, {"local_id": "local-1", "role": "implementer", "project_id": "demo"}, "canonical-1"
    )
    sync.assert_awaited_once()
    assert sync.call_args.args[1] == tmp_path / "proposals"
    scope = sync.call_args.kwargs
    assert scope["feature_id"] == "feature-1" and scope["task_id"] == "canonical-1"
    assert scope["principal"].actor_id == "task:canonical-1"
    assert scope["principal"].product_ids == ("product-1",)
    assert scope["principal"].project_ids == ("demo",)
