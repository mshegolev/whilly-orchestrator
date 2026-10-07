from datetime import datetime, timezone

import pytest

from whilly.swarm.learning.domain import ContextPackage, KnowledgeRevision, Principal, SourceCheck


def revision(**overrides):
    values = {
        "id": "r1",
        "product_id": "p1",
        "project_id": "proj1",
        "kind": "fact",
        "body": "body",
        "source_uri": "https://example.test/source",
        "source_sha": None,
        "evidence_hash": "eh",
        "observed_at": datetime.now(timezone.utc),
        "verified_at": None,
        "expires_at": None,
        "classification": "internal",
        "status": "candidate",
        "author_id": "a1",
        "verifier_id": None,
        "policy_version": "v1",
    }
    values.update(overrides)
    return KnowledgeRevision(**values)


def test_domain_models_are_frozen_and_validate_contract():
    item = revision()
    assert item.id == "r1"
    with pytest.raises((AttributeError, TypeError)):
        item.body = "changed"
    with pytest.raises(ValueError):
        revision(observed_at=datetime.now())
    with pytest.raises(ValueError):
        revision(body="x" * 16001)
    assert Principal("a", ("p",), ("project",), ("internal",)).actor_id == "a"
    assert SourceCheck("verified", "r1").revision == "r1"
    assert ContextPackage((item,), (), (), ("r1",)).revision_manifest == ("r1",)


def test_product_wide_revision_has_no_project_requirement_but_keeps_identity():
    item = revision(project_id=None)
    assert item.project_id is None
    with pytest.raises(ValueError):
        revision(classification="")
