"""Unit tests for :class:`whilly.audit.JsonlEventSink`.

Covers the v4.3.1 backwards-compatibility contract for
``whilly_logs/whilly_events.jsonl`` (VAL-CROSS-BACKCOMPAT-907):

* Sink writes one JSON-parseable line per :meth:`record` call.
* Each line carries the canonical ``ts`` / ``event`` / ``event_type``
  / ``task_id`` / ``plan_id`` / ``payload`` keys.
* ``payload`` mirrors the dict the caller supplied, byte-for-byte.
* Append semantics: subsequent calls extend the file rather than
  truncating it.
* Resilience: missing parent directory, env-var override, and write
  failures are handled gracefully — never raised.
* ``WHILLY_LOG_DIR`` env-var override is honoured.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from whilly.audit import (
    DEFAULT_JSONL_FILENAME,
    DEFAULT_LOG_DIR,
    LOG_DIR_ENV,
    JsonlEventSink,
    make_jsonl_sink_from_env,
)


# ── Construction ──────────────────────────────────────────────────────


def test_default_log_dir_constants_match_legacy_paths() -> None:
    """The contract pins the v3 / v4.3.1 paths verbatim.

    Operators tail ``whilly_logs/whilly_events.jsonl`` literally — any
    drift here would silently break their pipelines.
    """
    assert DEFAULT_LOG_DIR == "whilly_logs"
    assert DEFAULT_JSONL_FILENAME == "whilly_events.jsonl"
    assert LOG_DIR_ENV == "WHILLY_LOG_DIR"


def test_init_uses_default_log_dir_when_arg_and_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``log_dir=None`` and no env var → default ``whilly_logs/``."""
    monkeypatch.delenv(LOG_DIR_ENV, raising=False)
    sink = JsonlEventSink()
    assert sink.log_dir == Path(DEFAULT_LOG_DIR)
    assert sink.path == Path(DEFAULT_LOG_DIR) / DEFAULT_JSONL_FILENAME


def test_init_respects_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``WHILLY_LOG_DIR`` env-var overrides the default."""
    custom = tmp_path / "custom-logs"
    monkeypatch.setenv(LOG_DIR_ENV, str(custom))
    sink = JsonlEventSink()
    assert sink.log_dir == custom
    assert sink.path == custom / DEFAULT_JSONL_FILENAME


def test_init_explicit_arg_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit ``log_dir`` constructor argument trumps the env var."""
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "from-env"))
    explicit = tmp_path / "from-arg"
    sink = JsonlEventSink(log_dir=explicit)
    assert sink.log_dir == explicit


def test_init_does_not_create_file_or_dir(tmp_path: Path) -> None:
    """``__init__`` is side-effect-free — file/dir creation deferred to ``record``."""
    target = tmp_path / "deferred"
    sink = JsonlEventSink(log_dir=target)
    assert not target.exists(), "log_dir created at construction time"
    assert not sink.path.exists(), "JSONL file created at construction time"


def test_make_jsonl_sink_from_env_returns_jsonl_sink_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The factory returns a :class:`JsonlEventSink` bound to env / default."""
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    sink = make_jsonl_sink_from_env()
    assert isinstance(sink, JsonlEventSink)
    assert sink.log_dir == tmp_path


# ── record() — happy path ─────────────────────────────────────────────


def test_record_creates_log_dir_and_file_lazily(tmp_path: Path) -> None:
    """First ``record`` call creates the parent dir and the file."""
    target = tmp_path / "fresh" / "nested"
    sink = JsonlEventSink(log_dir=target)
    sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload={"version": 1})
    assert target.is_dir()
    assert sink.path.is_file()


def test_record_writes_one_json_parseable_line(tmp_path: Path) -> None:
    """Each ``record`` call appends exactly one ``json.loads``-able line."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record(
        "CLAIM",
        task_id="T-1",
        plan_id="P-1",
        payload={"worker_id": "w-x", "version": 1},
    )
    contents = sink.path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    lines = [line for line in contents.split("\n") if line]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)


def test_record_line_contains_canonical_keys(tmp_path: Path) -> None:
    """Line carries ``ts`` / ``event`` / ``event_type`` / ``task_id`` /
    ``plan_id`` / ``payload`` (VAL-CROSS-BACKCOMPAT-907 contract).
    """
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record(
        "COMPLETE",
        task_id="T-2",
        plan_id="P-2",
        payload={"version": 3, "usage": {"cost_usd": "0.0500"}},
    )
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert set(parsed.keys()) == {"ts", "event", "event_type", "task_id", "plan_id", "payload"}


def test_record_event_and_event_type_aliases_match(tmp_path: Path) -> None:
    """Legacy ``event`` and v4 ``event_type`` keys carry the same value."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record("FAIL", task_id="T-3", plan_id="P-3", payload={"reason": "x"})
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert parsed["event"] == "FAIL"
    assert parsed["event_type"] == "FAIL"


def test_record_ts_is_iso_utc(tmp_path: Path) -> None:
    """``ts`` is ISO-8601 UTC and parseable back to a tz-aware datetime."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload={})
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    ts = datetime.fromisoformat(parsed["ts"])
    assert ts.tzinfo is not None
    assert ts.tzinfo.utcoffset(ts) == timezone.utc.utcoffset(ts)


def test_record_payload_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """The serialised ``payload`` matches the input dict exactly."""
    sink = JsonlEventSink(log_dir=tmp_path)
    payload = {
        "worker_id": "w-1",
        "task_id": "T-1",
        "plan_id": "P-1",
        "claimed_at": "2026-05-01T12:00:00+00:00",
        "version": 7,
    }
    sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload=payload)
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert parsed["payload"] == payload


def test_record_payload_none_normalises_to_empty_dict(tmp_path: Path) -> None:
    """``payload=None`` (e.g. for plan-level events) → ``{}`` on the line."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record("plan.applied", task_id=None, plan_id="P-1", payload=None)
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert parsed["payload"] == {}


def test_record_task_id_and_plan_id_can_both_be_none(tmp_path: Path) -> None:
    """Defaults for ``task_id``/``plan_id`` (``None``) round-trip cleanly."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record("plan.budget_exceeded", payload={"plan_id": "P-1"})
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert parsed["task_id"] is None
    assert parsed["plan_id"] is None
    assert parsed["payload"] == {"plan_id": "P-1"}


# ── record() — append semantics ───────────────────────────────────────


def test_record_appends_multiple_lines_in_order(tmp_path: Path) -> None:
    """Three calls produce three lines preserving invocation order."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload={"version": 1})
    sink.record("START", task_id="T-1", plan_id=None, payload={"version": 2})
    sink.record("COMPLETE", task_id="T-1", plan_id="P-1", payload={"version": 3})
    raw = sink.path.read_text(encoding="utf-8")
    lines = [line for line in raw.split("\n") if line]
    assert [json.loads(line)["event_type"] for line in lines] == ["CLAIM", "START", "COMPLETE"]


def test_record_does_not_truncate_pre_existing_content(tmp_path: Path) -> None:
    """A second ``JsonlEventSink`` over the same path appends — never truncates."""
    target = tmp_path / "logs"
    target.mkdir()
    seed = target / DEFAULT_JSONL_FILENAME
    seed.write_text('{"event_type":"PRE-EXISTING"}\n', encoding="utf-8")
    sink = JsonlEventSink(log_dir=target)
    sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload={"version": 1})
    raw = seed.read_text(encoding="utf-8")
    assert raw.startswith('{"event_type":"PRE-EXISTING"}\n')
    assert raw.endswith("\n")
    lines = [line for line in raw.split("\n") if line]
    assert len(lines) == 2


# ── record() — failure modes ──────────────────────────────────────────


def test_record_swallows_mkdir_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-creatable parent directory logs a warning but never raises."""
    target = tmp_path / "no-perm"
    sink = JsonlEventSink(log_dir=target)

    def _raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated permission denied")

    monkeypatch.setattr(Path, "mkdir", _raise_oserror)
    with caplog.at_level("WARNING", logger="whilly.audit.jsonl_sink"):
        sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload={"version": 1})
    assert any("mkdir" in rec.getMessage() for rec in caplog.records)
    assert not sink.path.exists()


def test_record_swallows_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-writable file logs a warning but never raises."""
    sink = JsonlEventSink(log_dir=tmp_path)

    real_open = Path.open

    def _raise_on_append(self: Path, *args: object, **kwargs: object) -> object:  # type: ignore[no-untyped-def]
        if args and args[0] == "a":
            raise OSError("simulated disk full")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", _raise_on_append)
    with caplog.at_level("WARNING", logger="whilly.audit.jsonl_sink"):
        sink.record("CLAIM", task_id="T-1", plan_id="P-1", payload={"version": 1})
    assert any("write" in rec.getMessage() for rec in caplog.records)


def test_record_handles_unicode_payload(tmp_path: Path) -> None:
    """Non-ASCII payload values survive the round-trip (``ensure_ascii=False``)."""
    sink = JsonlEventSink(log_dir=tmp_path)
    sink.record(
        "FAIL",
        task_id="T-1",
        plan_id="P-1",
        payload={"reason": "не получилось 🤷"},
    )
    parsed = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert parsed["payload"]["reason"] == "не получилось 🤷"
