from __future__ import annotations

from pathlib import Path
import os
import socket
import subprocess
import io
import json
import time
from types import SimpleNamespace

import pytest

from whilly.adapters.memory.cognee_process import CogneeRetriever
from whilly.adapters.memory.cognee_sandbox import allowed_environment, sandbox_command
from whilly.adapters.memory import cognee_worker
from whilly.swarm.learning.retrieval import RetrievalRecord, RetrievalRequest


def test_record_body_is_text_even_when_it_looks_like_a_path():
    for body in ("/etc/passwd", "https://example.invalid/record", "plain record"):
        stream = cognee_worker._record_stream(body)
        assert stream.file.read() == body.encode("utf-8")
        assert stream.filename.endswith(".txt")


@pytest.mark.parametrize(
    "patch",
    [
        {"version": True},
        {"version": 1.0},
        {"records": ["text"]},
        {"records": [{"id": "a", "body": "b", "extra": "x"}]},
        {"records": [{"id": 1, "body": "b"}]},
        {"records": [{"id": "a", "body": None}]},
        {"records": [{"id": "a", "body": "b"}] * 2},
        {"records": [{"id": "a", "body": "x" * 131072}]},
    ],
)
def test_invalid_request_rejected_before_sdk(patch, monkeypatch):
    payload = {"version": 1, "query": "q", "records": []} | patch
    monkeypatch.setattr(cognee_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))

    def forbidden(_):
        pytest.fail("invalid request reached SDK")

    monkeypatch.setattr(cognee_worker, "_rank_with_cognee", forbidden)
    assert cognee_worker.main() == 2


def test_mapping_uses_chunk_provenance_not_body():
    hits = [
        SimpleNamespace(source="graph", kind="chunk", metadata={"data_id": "doc-b"}, raw={}, text="identical"),
        SimpleNamespace(source="graph", kind="chunk", metadata={"data_id": "doc-a"}, raw={}, text="identical"),
        SimpleNamespace(source="graph", kind="chunk", metadata={"data_id": "doc-long"}, raw={}, text="small chunk"),
        SimpleNamespace(source="graph", kind="chunk", metadata={"data_id": "doc-long"}, raw={}, text="another chunk"),
    ]
    assert cognee_worker._map_hits(hits, {"doc-a": "a", "doc-b": "b", "doc-long": "long"}) == ["b", "a", "long"]


def test_mapping_rejects_unknown_provenance_and_generated_answer():
    for hit in [
        SimpleNamespace(source="graph", kind="chunk", metadata={"data_id": "foreign"}, raw={}, text="a"),
        SimpleNamespace(source="graph", kind="graph_completion", metadata={"data_id": "doc-a"}, raw={}, text="a"),
    ]:
        with pytest.raises(RuntimeError):
            cognee_worker._map_hits([hit], {"doc-a": "a"})


@pytest.mark.asyncio
async def test_worker_round_trip_returns_real_match(tmp_path: Path) -> None:
    python_path = os.environ.get("WHILLY_COGNEE_PYTHON")
    models_path = os.environ.get("WHILLY_COGNEE_MODELS_DIR")
    if not python_path or not models_path:
        pytest.skip("controller-provided Cognee runtime/models are absent")
    python = Path(python_path)
    runtime = python.parent.parent
    models = Path(models_path)
    assert python.is_file() and models.is_dir(), "configured runtime/models must exist"
    retriever = CogneeRetriever(runtime, models_dir=models)
    result = await retriever.rank(
        RetrievalRequest(
            "find the incident with the stable opaque identifier",
            (
                RetrievalRecord("known", "stable opaque identifier incident details"),
                RetrievalRecord("same-body-a", "shared text that must not identify a record"),
                RetrievalRecord("same-body-b", "shared text that must not identify a record"),
            ),
        ),
        request_dir=tmp_path,
        deadline=time.monotonic() + 90,
    )
    assert "known" in result.ids
    assert set(result.ids) <= {"known", "same-body-a", "same-body-b"}
    assert result.elapsed_ms >= 0
    assert result.elapsed_ms < 90000
    assert retriever.last_child_alive is False


def _run_sandbox_probe(
    *, runtime: Path, request_dir: Path, models_dir: Path, probe: str
) -> subprocess.CompletedProcess[str]:
    python = runtime / "bin" / "python"
    command = sandbox_command(
        [str(python), "-c", probe],
        runtime=runtime,
        request_dir=request_dir,
        model_dirs=(models_dir,),
        worker_dir=request_dir,
    )
    return subprocess.run(
        command,
        cwd=request_dir,
        env=allowed_environment(request_dir, models_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_sandbox_denies_network_and_foreign_file(tmp_path: Path) -> None:
    python_path = os.environ.get("WHILLY_COGNEE_PYTHON")
    models_path = os.environ.get("WHILLY_COGNEE_MODELS_DIR")
    if not python_path or not models_path:
        pytest.skip("controller-provided Cognee runtime/models are absent")
    runtime = Path(python_path).parent.parent
    models = Path(models_path)
    assert Path(python_path).is_file() and models.is_dir(), "configured runtime/models must exist"

    foreign = tmp_path.parent / "foreign-sandbox-probe.txt"
    foreign.write_text("must stay unreadable", encoding="utf-8")
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    positive = _run_sandbox_probe(
        runtime=runtime,
        request_dir=request_dir,
        models_dir=models,
        probe="import sys; print(sys.prefix)",
    )
    assert positive.returncode == 0, positive.stderr
    assert Path(positive.stdout.strip()).resolve() == runtime.resolve()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    with socket.create_connection((host, port), timeout=1):
        accepted, _ = listener.accept()
        accepted.close()
    network_probe = f"""
import socket
import errno
try:
    socket.create_connection(({host!r}, {port}), timeout=1)
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
    print('network-denied')
    raise SystemExit(0)
raise SystemExit(31)
"""
    file_probe = f"""
from pathlib import Path
import errno
try:
    Path({str(foreign)!r}).read_text()
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
    print('file-denied')
    raise SystemExit(0)
raise SystemExit(32)
"""
    network = _run_sandbox_probe(runtime=runtime, request_dir=request_dir, models_dir=models, probe=network_probe)
    foreign_result = _run_sandbox_probe(runtime=runtime, request_dir=request_dir, models_dir=models, probe=file_probe)
    listener.close()
    assert network.returncode == 0, network.stderr
    assert network.stdout.strip() == "network-denied"
    assert foreign_result.returncode == 0, foreign_result.stderr
    assert foreign_result.stdout.strip() == "file-denied"

    model_file = next(path for path in models.rglob("config.json") if path.is_file())
    # Read-only targets are opened for writing without writing bytes: even a
    # regression in the sandbox cannot modify the installed runtime/models.
    paths = [str(foreign), str(model_file), str(Path(python_path).resolve())]
    (request_dir / "escape").symlink_to(foreign)
    probe = f"""
import errno
from pathlib import Path
Path('allowed.txt').write_text('request-only')
assert Path({str(model_file)!r}).read_bytes()
for name in {paths!r}:
    try:
        with open(name, 'r+b'):
            pass
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), error
    else:
        raise AssertionError('foreign write allowed')
try:
    Path('escape').read_bytes()
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
else:
    raise AssertionError('symlink escape')
print('readonly-and-request-write-ok')
"""
    protection = _run_sandbox_probe(runtime=runtime, request_dir=request_dir, models_dir=models, probe=probe)
    assert protection.returncode == 0, protection.stderr
    assert protection.stdout.strip() == "readonly-and-request-write-ok"


def test_environment_does_not_inherit_secrets_or_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "foreign"))
    before = os.environ.get("HOME")
    env = allowed_environment(tmp_path, tmp_path / "models")
    assert "HOME" not in env
    assert os.environ.get("HOME") == before
    assert "OPENAI_API_KEY" not in env and "PYTHONPATH" not in env
    assert env["HF_HUB_OFFLINE"] == "1"
