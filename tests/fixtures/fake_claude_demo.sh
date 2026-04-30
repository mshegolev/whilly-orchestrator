#!/bin/sh
#
# Demo-only Claude CLI stub — same envelope as fake_claude.sh, but with a
# 2.5s sleep so the parallel-claim frame is actually visible during a
# presentation (otherwise both tasks finish in <100ms on a fast laptop and
# the audience sees only the "after" snapshot).
#
# Used by docker-compose.demo.yml's worker service via the CLAUDE_BIN env
# var. The unit / integration tests still use the instant fake_claude.sh
# next to it — this stub is *only* for the workshop / DEMO-CHECKLIST flow.
#
# Override the delay with FAKE_CLAUDE_DEMO_DELAY (in seconds, integer or
# fractional) — useful if you want a longer "money frame" for video
# capture, or want the demo to be snappier than the default.

set -eu

DELAY="${FAKE_CLAUDE_DEMO_DELAY:-2.5}"

# busybox sh in postgres:15-alpine has fractional sleep; bash on debian:slim
# also handles fractional. POSIX `sleep` doesn't strictly require fractions
# but every shell we ship with does.
sleep "$DELAY"

cat <<'JSON'
{"result": "Task complete. <promise>COMPLETE</promise>", "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
JSON
