#!/usr/bin/env python3
"""Idempotent baseline-fixture generator for the v5 mission's M1 readiness gate.

Re-running this script on an already-initialised checkout is a no-op:

* Existing fixture / baseline / state files whose contents already match the
  canonical bytes embedded below are left alone.
* Distributed-audit reports under ``.planning/distributed-audit/`` are mirrored
  byte-for-byte into ``docs/distributed-audit/`` AND ``library/distributed-audit/``
  only if the destination is missing or has drifted. The ``library/`` location
  is the canonical mirror required by VAL-M1-DOCS-004 / VAL-M1-COMPOSE-902;
  the ``docs/`` mirror is retained for backwards-compatibility with the
  m1-readiness-baseline feature that introduced it.

The four canonical artifacts produced are:

1. ``tests/fixtures/v3_tasks.json``  — pre-key_files representative plan.
2. ``tests/fixtures/v4_tasks.json``  — v4.0-era plan with ``key_files`` +
   ``dependencies`` + ``plan_id``.
3. ``tests/fixtures/baselines/events_payload_v4.3.1.json`` — JSON-Schema-shaped
   reference document capturing the per-event_type ``events.payload`` shapes
   actually emitted at v4.3.1, so v4.4+ regression suites can diff against it.
4. ``tests/fixtures/whilly_state-v4.3.json`` — frozen snapshot of a
   :class:`whilly.state_store.StateStore`-emitted state file with all
   v4.3-era fields (``plan_file``, ``iteration``, ``cost_usd``,
   ``active_agents``, ``task_status``, ``paused``, ``pause_reason``,
   ``paused_at``, ``saved_at``).

Plus the four-plus distributed-audit reports under
``.planning/distributed-audit/`` mirrored to ``docs/distributed-audit/`` for
permanent reference at the canonical docs path.

Run:

    python3 scripts/m1_baseline_fixtures.py

The script prints one line per file describing whether it was ``created``,
``updated`` (drift fixed), or ``unchanged``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Canonical fixture content (embedded so the script is self-sufficient).
# ---------------------------------------------------------------------------

V3_TASKS = {
    "project": "v3-fixture-sample",
    "prd_file": "PRD-v3-sample.md",
    "created_at": "2025-08-12T00:00:00Z",
    "agent_instructions": {
        "before": [
            "Прочитай tasks.json и progress.txt прежде чем начать.",
            "Работай только над задачей с указанным id.",
        ],
        "after": [
            "Если test_steps не прошли, не помечай задачу done.",
            "Допиши <promise>COMPLETE</promise> в конец отчёта только при успехе.",
        ],
    },
    "tasks": [
        {
            "id": "TASK-001",
            "phase": "Phase 1 — Bootstrap",
            "category": "doc",
            "priority": "high",
            "description": "Pre-key_files representative task — only legacy fields.",
            "status": "pending",
            "dependencies": [],
            "acceptance_criteria": [
                "Documentation file produced at the location agreed upon in the PRD",
            ],
            "test_steps": [
                "test -s docs/v3-sample.md",
            ],
            "prd_requirement": "v3-step-1",
        },
        {
            "id": "TASK-002",
            "phase": "Phase 1 — Bootstrap",
            "category": "infra",
            "priority": "medium",
            "description": "Second representative task with a dependency, still no key_files.",
            "status": "pending",
            "dependencies": ["TASK-001"],
            "acceptance_criteria": [
                "Sequence runs after TASK-001 reaches done",
            ],
            "test_steps": [
                "grep -q 'TASK-001' tasks.json",
            ],
            "prd_requirement": "v3-step-2",
        },
        {
            "id": "TASK-003",
            "phase": "Phase 2 — Quality gate",
            "category": "test",
            "priority": "low",
            "description": "Smoke pytest demonstrating the v3 pipeline shape.",
            "status": "done",
            "dependencies": ["TASK-002"],
            "acceptance_criteria": [
                "pytest exits 0 against the produced module",
            ],
            "test_steps": [
                "pytest -q tests/test_v3_smoke.py",
            ],
            "prd_requirement": "v3-step-3",
        },
    ],
}


V4_TASKS = {
    "project": "v4-fixture-sample",
    "plan_id": "v4-fixture",
    "prd_file": "PRD-v4-sample.md",
    "created_at": "2026-01-10T00:00:00Z",
    "agent_instructions": {
        "before_start": [
            "Прочитай PRD-v4-sample.md полностью.",
            "Перед началом задачи проверь dependencies в tasks.json — все должны быть done.",
        ],
        "during_work": [
            "Следуй Hexagonal architecture: whilly/core/ — pure domain, whilly/adapters/ — I/O.",
            "После каждого редактирования Python файла запускай: ruff check --fix && ruff format.",
        ],
        "before_finish": [
            "Запусти pytest -q (все тесты должны проходить).",
            "Помечай <promise>COMPLETE</promise> только при полном выполнении AC.",
        ],
    },
    "tasks": [
        {
            "id": "TASK-001",
            "phase": "Day 1 — Skeleton",
            "category": "infra",
            "priority": "critical",
            "description": "v4.0-era representative task with key_files + dependencies.",
            "status": "pending",
            "dependencies": [],
            "key_files": [
                "whilly/core/__init__.py",
                "whilly/core/models.py",
            ],
            "acceptance_criteria": [
                "Module imports cleanly",
                "ruff check passes",
            ],
            "test_steps": [
                "ruff check whilly/core",
                "python -c 'import whilly.core'",
            ],
            "prd_requirement": "R-1",
        },
        {
            "id": "TASK-002",
            "phase": "Day 1 — Skeleton",
            "category": "infra",
            "priority": "high",
            "description": "Dependent task touching adapters layer with a non-overlapping key_files set.",
            "status": "pending",
            "dependencies": ["TASK-001"],
            "key_files": [
                "whilly/adapters/db/repository.py",
            ],
            "acceptance_criteria": [
                "Repository integration tests pass",
            ],
            "test_steps": [
                "pytest -q tests/integration/test_repository.py",
            ],
            "prd_requirement": "R-2",
        },
        {
            "id": "TASK-003",
            "phase": "Day 2 — Transport",
            "category": "feat",
            "priority": "medium",
            "description": "Transport-layer task with overlapping key_files (must NOT batch with TASK-002).",
            "status": "pending",
            "dependencies": ["TASK-001"],
            "key_files": [
                "whilly/adapters/db/repository.py",
                "whilly/adapters/transport/server.py",
            ],
            "acceptance_criteria": [
                "Endpoint returns 201 on register",
            ],
            "test_steps": [
                "pytest -q tests/integration/test_transport_register.py",
            ],
            "prd_requirement": "R-3",
        },
        {
            "id": "TASK-004",
            "phase": "Day 3 — Wrap-up",
            "category": "doc",
            "priority": "low",
            "description": "Documentation task with no overlap — eligible for parallel batching with TASK-003.",
            "status": "pending",
            "dependencies": ["TASK-002"],
            "key_files": [
                "docs/v4-fixture.md",
            ],
            "acceptance_criteria": [
                "Doc file exists and is non-empty",
            ],
            "test_steps": [
                "test -s docs/v4-fixture.md",
            ],
            "prd_requirement": "R-4",
        },
    ],
}


# Baseline JSON-Schema-shaped reference document for the v4.3.1 events.payload
# jsonb column, by event_type. Captured from the canonical sources:
#   whilly/adapters/db/repository.py (CLAIM/COMPLETE/FAIL/RELEASE/RESET +
#   plan.budget_exceeded + task.created + task.skipped + plan.applied)
#   whilly/api/event_flusher.py (triz.contradiction, triz.error)
EVENTS_PAYLOAD_BASELINE = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://github.com/mshegolev/whilly/schemas/events_payload_v4.3.1.json",
    "title": "Whilly v4.3.1 events.payload baseline",
    "description": (
        "Per-event_type baseline JSON-Schema for the events.payload jsonb column "
        "as emitted by Whilly v4.3.1. Future minor versions (v4.4+) MUST keep this "
        "schema as a backwards-compatible subset — additions are allowed, but "
        "required fields and field types must not change."
    ),
    "version": "4.3.1",
    "captured_from": [
        "whilly/adapters/db/repository.py",
        "whilly/api/event_flusher.py",
        "whilly/api/main.py",
        "whilly/worker/local.py",
    ],
    "event_types": {
        "CLAIM": {
            "type": "object",
            "required": ["worker_id", "version"],
            "properties": {
                "worker_id": {"type": "string"},
                "version": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": True,
        },
        "COMPLETE": {
            "type": "object",
            "required": ["version"],
            "properties": {
                "version": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": True,
        },
        "FAIL": {
            "type": "object",
            "required": ["version", "reason"],
            "properties": {
                "version": {"type": "integer", "minimum": 0},
                "reason": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "RELEASE": {
            "type": "object",
            "required": ["version", "reason"],
            "properties": {
                "version": {"type": "integer", "minimum": 0},
                "reason": {
                    "type": "string",
                    "enum": [
                        "visibility_timeout",
                        "worker_offline",
                        "manual_reset",
                        "timeout",
                        "shutdown",
                    ],
                },
            },
            "additionalProperties": True,
        },
        "RESET": {
            "type": "object",
            "required": ["reason"],
            "properties": {
                "version": {"type": "integer", "minimum": 0},
                "reason": {"type": "string", "enum": ["manual_reset"]},
                "mode": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "task.created": {
            "type": "object",
            "required": ["plan_id"],
            "properties": {
                "plan_id": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "task.skipped": {
            "type": "object",
            "required": ["reason"],
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["decision_gate_failed", "deadlock"],
                },
                "missing_requirements": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "additionalProperties": True,
        },
        "plan.applied": {
            "type": "object",
            "required": ["plan_id"],
            "properties": {
                "plan_id": {"type": "string"},
                "task_count": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": True,
        },
        "plan.budget_exceeded": {
            "type": "object",
            "required": ["plan_id"],
            "properties": {
                "plan_id": {"type": "string"},
                "threshold_pct": {"type": "number"},
                "cost_usd": {"type": "number"},
                "budget_usd": {"type": "number"},
            },
            "additionalProperties": True,
        },
        "triz.contradiction": {
            "type": "object",
            "required": ["contradiction_type"],
            "properties": {
                "contradiction_type": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "triz.error": {
            "type": "object",
            "properties": {
                "error": {"type": "string"},
            },
            "additionalProperties": True,
        },
    },
}


# A representative state-snapshot file as emitted by
# :meth:`whilly.state_store.StateStore.save` at v4.3.x. Fields are pinned to
# the canonical names so v4.4+ round-trip tests can detect schema drift.
WHILLY_STATE_V43 = {
    "plan_file": ".planning/v4-1_tasks.json",
    "iteration": 7,
    "cost_usd": 1.234,
    "active_agents": [
        {
            "task_id": "TASK-003",
            "session_name": "whilly-TASK-003",
            "started_at": 1714521600.0,
        },
        {
            "task_id": "TASK-004",
            "session_name": "whilly-TASK-004",
            "started_at": 1714521610.5,
        },
    ],
    "task_status": {
        "TASK-001": "done",
        "TASK-002": "done",
        "TASK-003": "in_progress",
        "TASK-004": "in_progress",
        "TASK-005": "pending",
        "TASK-006": "pending",
        "TASK-007": "skipped",
    },
    "paused": False,
    "pause_reason": "",
    "paused_at": None,
    "saved_at": 1714521700.0,
}


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _write_json_idempotent(path: Path, data: object) -> str:
    """Write ``data`` as pretty-printed JSON only if missing/drifted.

    Returns one of ``"created"``, ``"updated"``, ``"unchanged"``.
    """
    canonical = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == canonical:
            return "unchanged"
        path.write_text(canonical, encoding="utf-8")
        return "updated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical, encoding="utf-8")
    return "created"


def _mirror_file_idempotent(src: Path, dst: Path) -> str:
    """Copy ``src`` → ``dst`` only if missing or drifted (byte-equality).

    Returns one of ``"created"``, ``"updated"``, ``"unchanged"``.
    """
    src_bytes = src.read_bytes()
    if dst.exists():
        if dst.read_bytes() == src_bytes:
            return "unchanged"
        dst.write_bytes(src_bytes)
        return "updated"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return "created"


def main() -> int:
    actions: list[tuple[str, Path, str]] = []

    # 1. v3_tasks.json
    actions.append(
        (
            "v3_tasks",
            REPO_ROOT / "tests" / "fixtures" / "v3_tasks.json",
            _write_json_idempotent(REPO_ROOT / "tests" / "fixtures" / "v3_tasks.json", V3_TASKS),
        )
    )

    # 2. v4_tasks.json
    actions.append(
        (
            "v4_tasks",
            REPO_ROOT / "tests" / "fixtures" / "v4_tasks.json",
            _write_json_idempotent(REPO_ROOT / "tests" / "fixtures" / "v4_tasks.json", V4_TASKS),
        )
    )

    # 3. baselines/events_payload_v4.3.1.json
    actions.append(
        (
            "events_payload_v4.3.1",
            REPO_ROOT / "tests" / "fixtures" / "baselines" / "events_payload_v4.3.1.json",
            _write_json_idempotent(
                REPO_ROOT / "tests" / "fixtures" / "baselines" / "events_payload_v4.3.1.json",
                EVENTS_PAYLOAD_BASELINE,
            ),
        )
    )

    # 4. whilly_state-v4.3.json
    actions.append(
        (
            "whilly_state-v4.3",
            REPO_ROOT / "tests" / "fixtures" / "whilly_state-v4.3.json",
            _write_json_idempotent(
                REPO_ROOT / "tests" / "fixtures" / "whilly_state-v4.3.json",
                WHILLY_STATE_V43,
            ),
        )
    )

    # 5. Mirror .planning/distributed-audit/ -> docs/distributed-audit/
    #    AND -> library/distributed-audit/ (the canonical M1 location per
    #    VAL-M1-DOCS-004 / VAL-M1-COMPOSE-902). Both mirrors are byte-equal
    #    to the source, so future tooling can ``diff -r`` either against
    #    ``.planning/distributed-audit/`` and expect zero divergence.
    src_dir = REPO_ROOT / ".planning" / "distributed-audit"
    if not src_dir.is_dir():
        print(f"ERROR: source directory missing: {src_dir}", file=sys.stderr)
        return 2

    mirror_destinations: tuple[Path, ...] = (
        REPO_ROOT / "docs" / "distributed-audit",
        REPO_ROOT / "library" / "distributed-audit",
    )
    for dst_dir in mirror_destinations:
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(src_dir.iterdir()):
            if not src_file.is_file():
                continue
            dst_file = dst_dir / src_file.name
            mirror_label = dst_dir.relative_to(REPO_ROOT).as_posix()
            actions.append(
                (
                    f"{mirror_label}/{src_file.name}",
                    dst_file,
                    _mirror_file_idempotent(src_file, dst_file),
                )
            )

    # Print summary
    longest = max(len(name) for name, _, _ in actions)
    for name, path, status in actions:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {name:<{longest}}  {status:<9}  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
