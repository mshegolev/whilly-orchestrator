from pathlib import Path

import pytest

from whilly.swarm.product import ProductService
from whilly.api.product_swarm import SpecRequest


@pytest.mark.asyncio
async def test_prepare_binds_spec_revision_registry_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace
    from whilly.swarm import product as product_module

    class FakeStore:
        async def ensure_product(self, name: str):
            return {"id": "default", "name": name}

        async def create_feature(self, **kwargs):
            return {"id": "f1", **kwargs}

        async def prepare(self, feature_id: str, **kwargs):
            return {"id": feature_id, **kwargs}

    service = ProductService(object(), str(tmp_path / "registry.json"))
    service.store = FakeStore()
    monkeypatch.setattr(service, "_registry_snapshot", lambda: {"name": "demo", "hash": "r1"})
    monkeypatch.setattr(service, "_create_session", lambda title: _session(title))
    monkeypatch.setattr(
        product_module,
        "load_registry",
        lambda _path: SimpleNamespace(projects={"demo-lib": SimpleNamespace(path=str(tmp_path), execution=None)}),
    )

    result = await service.prepare(
        "f1",
        spec={"goal": "measure"},
        plan_revision=3,
        base_shas={"demo-lib": "abc"},
        profiles={"planner": "strong", "worker": "cheap"},
        budget={"planner_usd": 2, "worker_usd": 1},
    )

    assert result["registry_hash"] == "r1"
    assert result["plan_revision"] == 3


def _session(title: str) -> str:
    assert title
    return "s123"


def test_browser_spec_request_rejects_trusted_binding_fields() -> None:
    with pytest.raises(ValueError):
        SpecRequest(body={"goal": "x"}, plan_revision=1)
