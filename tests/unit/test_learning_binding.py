from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whilly.swarm.learning.domain import ContextPackage, KnowledgeRevision, Principal, SourceCheck
from whilly.swarm.learning_binding import bind_spec, make_binding, validate_binding


def test_memory_backend_defaults_to_l1_and_does_not_resolve_python(monkeypatch, tmp_path):
    from whilly.adapters.memory.config import memory_config

    monkeypatch.delenv("WHILLY_MEMORY_BACKEND", raising=False)
    monkeypatch.setenv("WHILLY_COGNEE_PYTHON", str(tmp_path / "bin" / "python"))
    config = memory_config()
    assert config.backend == "l1"
    assert config.runtime is None


def test_cognee_runtime_root_preserves_symlink_path(monkeypatch, tmp_path):
    from whilly.adapters.memory.config import memory_config

    venv = tmp_path / "cognee-venv"
    python = tmp_path / "python-link"
    (venv / "bin").mkdir(parents=True)
    python.symlink_to(venv / "bin" / "python")
    monkeypatch.setenv("WHILLY_MEMORY_BACKEND", "cognee")
    monkeypatch.setenv("WHILLY_COGNEE_PYTHON", str(python))
    monkeypatch.setenv("WHILLY_COGNEE_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("WHILLY_COGNEE_STATE_DIR", str(tmp_path / "state"))
    config = memory_config()
    assert config.runtime == python.absolute().parent.parent


def test_cognee_readiness_accepts_documented_nested_model_layout(monkeypatch, tmp_path):
    from whilly.adapters.memory.config import MemoryConfig, MemoryCoordinator

    runtime = tmp_path / "cognee-venv"
    python = runtime / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    models = tmp_path / "models"
    hub_snapshot = models / "hub" / "models--fastino--gliner2.5-base-v1" / "snapshots" / "hub-sha"
    fastembed_snapshot = models / "fastembed" / "models--qdrant--bge-small-en-v1.5-onnx-q" / "snapshots" / "embed-sha"
    hub_snapshot.mkdir(parents=True)
    fastembed_snapshot.mkdir(parents=True)
    (hub_snapshot / "config.json").write_text("{}", encoding="utf-8")
    (hub_snapshot / "model.safetensors").write_bytes(b"weights")
    (fastembed_snapshot / "config.json").write_text("{}", encoding="utf-8")
    (fastembed_snapshot / "model_optimized.onnx").write_bytes(b"onnx")
    config = MemoryConfig("cognee", runtime, models, tmp_path / "state")
    coordinator = MemoryCoordinator(object(), config, object())
    monkeypatch.setattr("whilly.adapters.memory.config.sys.platform", "darwin")
    monkeypatch.setattr("whilly.adapters.memory.config.shutil.which", lambda name: "/usr/bin/sandbox-exec")
    assert coordinator.status().ready
    assert coordinator.status().error_code is None
    (fastembed_snapshot / "model_optimized.onnx").unlink()
    assert coordinator.status().error_code == "models_config_missing"


def test_memory_coordinator_cache_separates_state_roots(monkeypatch, tmp_path):
    from whilly.adapters.memory.config import build_memory_coordinator

    registry = type(
        "Registry",
        (),
        {"source_path": "registry.json", "projects": {"p": type("Project", (), {"path": ".", "base_ref": "main"})()}},
    )()
    pool = object()
    monkeypatch.setenv("WHILLY_MEMORY_BACKEND", "l1")
    monkeypatch.setenv("WHILLY_COGNEE_STATE_DIR", str(tmp_path / "state-a"))
    first = build_memory_coordinator(pool, registry)
    monkeypatch.setenv("WHILLY_COGNEE_STATE_DIR", str(tmp_path / "state-b"))
    second = build_memory_coordinator(pool, registry)
    assert first is not second


def test_unknown_memory_backend_is_named(monkeypatch):
    from whilly.adapters.memory.config import memory_config

    monkeypatch.setenv("WHILLY_MEMORY_BACKEND", "remote")
    with pytest.raises(ValueError, match="memory_backend_invalid"):
        memory_config()


@pytest.mark.asyncio
async def test_transient_cognee_failure_does_not_poison_retry(monkeypatch):
    from whilly.adapters.memory.config import MemoryConfig, MemoryCoordinator
    from whilly.swarm.learning.retrieval import RetrievalUnavailable

    class Lookup:
        def __init__(self):
            self.calls = 0

        async def context(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RetrievalUnavailable("timeout")
            return ContextPackage((), (), (), ())

    class L1:
        async def context(self, *args, **kwargs):
            return ContextPackage((), (), (), ())

    lookup = Lookup()
    lookup._leases = type("Leases", (), {"boot_cleanup": lambda self: _done()})()
    config = MemoryConfig("cognee", None, None, Path("state"))
    coordinator = MemoryCoordinator(lookup, config, L1())
    monkeypatch.setattr(coordinator, "_readiness_error", lambda: None)
    args = (Principal("a", ("p",), ("x",), ("internal",)), "p", ("x",), "q")
    with pytest.raises(RetrievalUnavailable, match="timeout"):
        await coordinator.context(*args, max_chars=1000)
    assert coordinator.status().ready
    assert coordinator.status().error_code == "timeout"
    assert await coordinator.context(*args, max_chars=1000) == ContextPackage((), (), (), ())
    assert coordinator.status().ready
    assert coordinator.status().error_code is None


async def _done():
    return ()


def item():
    return KnowledgeRevision(
        id="r1",
        product_id="default",
        project_id="demo",
        kind="fact",
        body="secret",
        source_uri="git:README.md",
        source_sha="a" * 40,
        evidence_hash="e1",
        observed_at=datetime.now(timezone.utc),
        verified_at=datetime.now(timezone.utc),
        expires_at=None,
        classification="internal",
        status="verified",
        author_id="a",
        verifier_id="v",
        policy_version="1",
    )


def test_binding_contains_hashes_not_body():
    binding = make_binding(ContextPackage((item(),), (), (), ("r1",)))
    assert binding["revisions"][0]["id"] == "r1"
    assert "body" not in binding["revisions"][0]


def test_bind_spec_preserves_artifact_fields():
    value = bind_spec({"document": "x", "artifacts": {"a.md": "y"}}, {"revisions": []})
    assert value["artifacts"] == {"a.md": "y"}
    assert value["memory_binding"] == {"revisions": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expires", "conflicts", "retracted", "source_unavailable"])
async def test_bound_revision_loses_eligibility(monkeypatch, change):
    from whilly.swarm import learning_binding
    from whilly.swarm.registry import Project, Registry

    current = item()
    if change == "expires":
        current = replace(current, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    elif change == "conflicts":
        current = replace(current, conflicts=("other",))
    elif change == "retracted":
        current = replace(current, status="retracted")
    binding = make_binding(ContextPackage((item(),), (), (), ("r1",)))

    class Store:
        def __init__(self, pool):
            pass

        async def visible(self, *args):
            return [current]

    class Verifier:
        def __init__(self, projects):
            pass

        async def check(self, revision):
            return SourceCheck("unavailable", None)

    monkeypatch.setattr(learning_binding, "PostgresMemoryStore", Store)
    monkeypatch.setattr(learning_binding, "GitSourceVerifier", Verifier)
    registry = Registry("test", "test", {"demo": Project("demo", ".", "main", "demo")}, {})
    with pytest.raises(PermissionError):
        await validate_binding(None, registry, "default", binding)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_uri, source_sha, expires_at", [("git:README.md", None, None), ("https://example.test/fact", None, None)]
)
async def test_bound_source_without_verification_fails_closed(monkeypatch, source_uri, source_sha, expires_at):
    from whilly.swarm import learning_binding
    from whilly.swarm.registry import Project, Registry

    current = replace(item(), source_uri=source_uri, source_sha=source_sha, expires_at=expires_at)
    binding = make_binding(ContextPackage((current,), (), (), ("r1",)))

    class Store:
        def __init__(self, pool):
            pass

        async def visible(self, *args):
            return [current]

    class Verifier:
        def __init__(self, projects):
            pass

        async def check(self, revision):
            return SourceCheck("verified", revision.source_sha)

    monkeypatch.setattr(learning_binding, "PostgresMemoryStore", Store)
    monkeypatch.setattr(learning_binding, "GitSourceVerifier", Verifier)
    registry = Registry("test", "test", {"demo": Project("demo", ".", "main", "demo")}, {})
    with pytest.raises(PermissionError):
        await validate_binding(None, registry, "default", binding)


@pytest.mark.asyncio
async def test_unrelated_revision_keeps_binding(monkeypatch):
    from whilly.swarm import learning_binding
    from whilly.swarm.registry import Project, Registry

    bound = item()
    unrelated = replace(bound, id="other", evidence_hash="other-evidence")

    class Store:
        def __init__(self, pool):
            pass

        async def visible(self, *args):
            return [bound, unrelated]

    class Verifier:
        def __init__(self, projects):
            pass

        async def check(self, revision):
            return SourceCheck("verified", revision.source_sha)

    monkeypatch.setattr(learning_binding, "PostgresMemoryStore", Store)
    monkeypatch.setattr(learning_binding, "GitSourceVerifier", Verifier)
    registry = Registry("test", "test", {"demo": Project("demo", ".", "main", "demo")}, {})
    await validate_binding(None, registry, "default", make_binding(ContextPackage((bound,), (), (), ("r1",))))
