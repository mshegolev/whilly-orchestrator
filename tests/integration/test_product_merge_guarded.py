"""Hermetic merge/revert effects through the pinned GitLab boundary."""

from __future__ import annotations

import socket

import httpx
import pytest

from tests.unit.test_gitlab_change_transport import SECRET, SHA, TARGET, Git, policy
from whilly.swarm.gitlab_change_transport import GitLabChangeTransport, PinnedGitLabHTTPS, PublicationError
from whilly.swarm.product_merge import MergeRepositoryBinding


class Store:
    def __init__(self):
        self.receipts = {}

    async def get_effect(self, key):
        return self.receipts.get(key)

    async def record_effect(self, receipt):
        self.receipts.setdefault(receipt.effect_key, receipt)
        return self.receipts[receipt.effect_key]


class API:
    def __init__(self, *, wrong_merge_sha=False):
        self.calls = []
        self.wrong_merge_sha = wrong_merge_sha

    def __call__(self, request):
        self.calls.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path.endswith("/merge_requests/7/merge"):
            body = {"iid": 7, "state": "merged", "sha": "c" * 40 if self.wrong_merge_sha else SHA,
                    "merge_commit_sha": "9" * 40}
        elif path.endswith("/merge_requests/7/revert"):
            body = {"branch": "whilly/revert/change-demo/demo", "commit": {"id": "8" * 40}}
        elif path.endswith("/merge_requests"):
            body = {"iid": 107, "source_branch": "whilly/revert/change-demo/demo", "target_branch": "master",
                    "sha": "8" * 40, "state": "opened"}
        elif path.endswith("/merge_requests/107/merge"):
            body = {"iid": 107, "state": "merged", "sha": "8" * 40, "merge_commit_sha": "7" * 40}
        else:
            raise AssertionError(path)
        return httpx.Response(200 if request.method == "PUT" else 201, json=body)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("real network forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("DNS forbidden"))


def adapter(api):
    store = Store()
    http = PinnedGitLabHTTPS(
        "https://gitlab.example.com", allowed_project_ids=frozenset({17}),
        credentials=lambda _: SECRET, transport=httpx.MockTransport(api),
    )
    return GitLabChangeTransport(Git(), http, effect_store=store), store


def binding():
    return MergeRepositoryBinding("demo", 17, SHA, TARGET, "whilly/change-demo", 7, 31, ("test", "build"))


async def test_merge_and_revert_are_idempotent_receipted_effects():
    api = API()
    transport, store = adapter(api)
    merged = await transport.merge_request("change-demo", policy(), binding())
    assert merged == {"mr_iid": 7, "source_sha": SHA, "merge_commit_sha": "9" * 40}
    assert await transport.merge_request("change-demo", policy(), binding()) == merged
    reverted = await transport.revert_merge_request("change-demo", policy(), binding(), merged)
    assert reverted["mr_iid"] == 107 and reverted["revert_commit_sha"] == "7" * 40
    assert await transport.revert_merge_request("change-demo", policy(), binding(), merged) == reverted
    assert [method for method, _path, _body in api.calls] == ["PUT", "POST", "POST", "PUT"]
    assert {receipt.operation for receipt in store.receipts.values()} == {
        "merge_intent", "merge", "revert_intent", "revert",
    }


async def test_merge_response_must_preserve_exact_source_identity():
    api = API(wrong_merge_sha=True)
    transport, _ = adapter(api)
    with pytest.raises(PublicationError, match="merge_effect_requires_reconciliation"):
        await transport.merge_request("change-demo", policy(), binding())
    assert len(api.calls) == 1


async def test_merge_boundary_rejects_unallowlisted_mutations():
    _transport, _ = adapter(API())
    http = _transport.http
    for method, path in (
        ("DELETE", "/api/v4/projects/17/merge_requests/7"),
        ("PUT", "/api/v4/projects/17/repository/branches/master"),
        ("POST", "/api/v4/projects/17/repository/commits"),
    ):
        with pytest.raises(PublicationError, match="publication_endpoint_blocked"):
            await http.request(method, path, json={})
