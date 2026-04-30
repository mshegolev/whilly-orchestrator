"""Forge — GitHub Issue → PR autonomous pipeline (TASK-108a).

Stage 1 (this package, TASK-108a): **intake** — fetch a GitHub Issue
via the ``gh`` CLI, normalise it into a Whilly plan in Postgres, flip
the issue label from ``whilly-pending`` to ``whilly-in-progress``.

Stage 2 (TASK-108b, future): **compose** — once the plan's tasks all
land DONE, aggregate the results, open a PR, flip the label to
``whilly-in-review``.

Public surface
--------------
The intake CLI is exposed via ``whilly forge intake owner/repo/<N>``.
The Python entry point :func:`whilly.forge.intake.run_forge_intake_command`
is what :mod:`whilly.cli` dispatches to.

Why a sub-package and not a single module?
    Forge has two stages with separate concerns and separate test
    surfaces. Putting them in their own package keeps each module
    short and lets future stages grow without bloating a single file.
"""

from __future__ import annotations

from whilly.forge.intake import run_forge_intake_command

__all__ = ["run_forge_intake_command"]
