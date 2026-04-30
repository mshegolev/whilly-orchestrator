#!/bin/sh
#
# Deterministic ``gh`` CLI stub for the Forge intake integration tests
# (TASK-108a, paired with tests/integration/test_forge_intake.py).
#
# Most unit tests prefer to monkeypatch ``whilly.forge._gh._run_gh``
# directly because that's a single Python seam — this stub exists for
# the rare integration smoke that drives the full ``whilly forge intake``
# subprocess via ``python -m whilly.cli`` and needs an executable on
# ``PATH`` rather than a Python monkeypatch. Reach for it the same way
# the existing ``fake_claude_prd.sh`` / ``fake_claude.sh`` fixtures do:
# point ``PATH``-prepend or a ``GH_BIN`` override at this script.
#
# Behaviour summary
# -----------------
# Recognised invocation shapes:
#
#   gh issue view <N> --repo OWNER/REPO --json <fields>
#       Emits a stable issue payload (number = N, title = test issue,
#       body containing the issue ref, single comment, label set =
#       ['whilly-pending'], state = OPEN, url = https://github.com/...).
#
#   gh issue edit <N> --repo OWNER/REPO --remove-label X --add-label Y
#       Emits ``"https://github.com/OWNER/REPO/issues/N"`` (matching
#       the real ``gh issue edit`` output) and exits 0. Intake-time
#       label transitions land here.
#
# Anything else exits 1 so a regression in the call shape fails loudly
# rather than silently masquerading as success.
#
# Usage
# -----
#   chmod +x tests/fixtures/fake_gh.sh
#   PATH="$(pwd)/tests/fixtures:$PATH" \
#       pytest tests/integration/test_forge_intake.py -v
#
# Tests typically prefer the lighter monkeypatch route — this stub is
# here for cases (manual smoke, debugging) where a real PATH-prepended
# binary is needed.

set -eu

if [ "$#" -lt 1 ]; then
    echo "fake_gh.sh: missing subcommand" >&2
    exit 1
fi

case "$1 $2" in
    "issue view")
        # Find the issue number — first non-flag positional after
        # ``view``. POSIX shell doesn't have a clean argv slicer; we
        # walk argv and pick up the first arg that starts with a digit.
        number=""
        for arg in "$@"; do
            case "$arg" in
                [0-9]*)
                    number="$arg"
                    break
                    ;;
            esac
        done
        if [ -z "$number" ]; then
            echo "fake_gh.sh: gh issue view requires a numeric issue id" >&2
            exit 1
        fi
        cat <<JSON
{
  "number": ${number},
  "title": "[mission-test] fake forge intake issue",
  "body": "Synthetic issue body emitted by fake_gh.sh.\n\nWe want a tiny smoke that drives whilly forge intake end-to-end.",
  "labels": [{"name": "whilly-pending"}],
  "comments": [
    {
      "body": "Comment body 1 — additional context the PRD wizard can fold into the description."
    }
  ],
  "state": "OPEN",
  "url": "https://github.com/example/repo/issues/${number}"
}
JSON
        exit 0
        ;;
    "issue edit")
        # Parse out the issue number (first numeric positional after
        # ``edit``) and echo the canonical ``gh issue edit`` happy-
        # path output (one URL on stdout, exit 0).
        number=""
        for arg in "$@"; do
            case "$arg" in
                [0-9]*)
                    number="$arg"
                    break
                    ;;
            esac
        done
        if [ -z "$number" ]; then
            echo "fake_gh.sh: gh issue edit requires a numeric issue id" >&2
            exit 1
        fi
        echo "https://github.com/example/repo/issues/${number}"
        exit 0
        ;;
    *)
        echo "fake_gh.sh: unsupported subcommand: $1 $2" >&2
        exit 1
        ;;
esac
