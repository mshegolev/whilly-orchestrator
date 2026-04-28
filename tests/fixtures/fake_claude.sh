#!/bin/sh
#
# Deterministic Claude CLI stub for Whilly v4.0 integration tests (TASK-020).
#
# Behaviour
# ---------
# Ignores every argument and the prompt content; always emits a single JSON
# envelope on stdout whose ``result`` field carries the
# ``<promise>COMPLETE</promise>`` marker that the worker loop interprets as
# successful task completion (PRD FR-1.6). Exits 0 so the runner classifies
# the result as DONE rather than FAILED (whilly/worker/local.py routes on
# ``is_complete && exit_code == 0``).
#
# Why a real binary, not a Python mock
# ------------------------------------
# The runner spawns ``claude`` via ``asyncio.create_subprocess_exec`` and
# parses its stdout — that's the actual seam between the worker and the
# agent. A Python-side mock would bypass ``parse_output()``, the JSON
# wire-shape, and the exit-code thread-through, defeating the point of an
# end-to-end test. A POSIX shell stub stays in the same lane the production
# binary uses.
#
# Usage
# -----
# Set ``CLAUDE_BIN=/abs/path/tests/fixtures/fake_claude.sh`` (or a relative
# path that resolves from the worker's cwd). The runner reads the env var
# at every spawn (whilly/adapters/runner/claude_cli.py::_claude_bin), so
# integration tests can monkeypatch it without restarting Python.
#
# This file MUST stay executable (chmod +x). The TASK-020 test asserts on
# ``os.access(..., os.X_OK)`` so a CI checkout that loses the bit fails
# loudly rather than silently degrading to ``EXIT_BINARY_NOT_FOUND``.

set -eu

# Single-envelope JSON, matching the shape ``parse_output()`` accepts:
# ``result`` carries the marker; ``usage``/``total_cost_usd``/``num_turns``
# /``duration_ms`` are zero-cost placeholders so the parser populates an
# AgentUsage rather than falling through to its raw-text fallback path.
cat <<'JSON'
{"result": "Task complete. <promise>COMPLETE</promise>", "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
JSON
