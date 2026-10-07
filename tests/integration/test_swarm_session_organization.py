"""Reversible session metadata against isolated PostgreSQL."""

# ruff: noqa: F811
from tests.integration.test_swarm_runtime import db_pool, ecosystem, service, swarm_dsn  # noqa: F401


async def test_flags_filter_and_restore_without_losing_history(service, ecosystem):
    sid = await service.create_session(str(ecosystem["registry"]), title="ordinary title")
    await service.store.add_chat_message(sid, "user", "Keep this history")
    await service.store.set_session_flags(sid, is_test=True)
    assert sid not in {s["id"] for s in await service.store.list_sessions()}
    assert sid in {s["id"] for s in await service.store.list_sessions(include_test=True)}
    await service.store.set_session_flags(sid, archived=True)
    status = await service.status(sid)
    assert status["session"]["archived"] is True
    assert status["session"]["is_test"] is True
    assert sid not in {s["id"] for s in await service.store.list_sessions(include_test=True)}
    assert sid in {s["id"] for s in await service.store.list_sessions(include_test=True, include_archived=True)}
    await service.store.set_session_flags(sid, archived=False, is_test=False)
    row = await service.store.get_session(sid)
    assert row["archived"] is False and row["is_test"] is False
    assert (await service.store.chat_history(sid))[0]["body"] == "Keep this history"
    assert sid in {s["id"] for s in await service.store.list_sessions()}
    assert await service.store.set_session_flags("s0000000000", archived=True) is None
