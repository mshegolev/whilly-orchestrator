"""Module-mode entry point for ``python -m whilly.cli`` (TASK-010b).

Mirrors the existing :mod:`whilly.__main__` so the v4 CLI is reachable both
as ``whilly ...`` (via the console script in :file:`pyproject.toml`) and as
``python -m whilly.cli ...`` for environments where the script isn't on
``$PATH`` (notably integration tests that exec a clean Python interpreter
via :class:`subprocess`).
"""

from __future__ import annotations

import sys

from whilly.cli import main

if __name__ == "__main__":
    sys.exit(main())
