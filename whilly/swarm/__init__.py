"""Opt-in local multi-project swarm (``whilly swarm``).

One persistent conversation plans bounded work across a registered set of
local Git projects. Approved plan revisions are imported into Whilly's own
``plans`` / ``tasks`` queue and executed by a single session coordinator that
claims tasks through :class:`whilly.adapters.db.repository.TaskRepository`.

Module map:

* :mod:`whilly.swarm.registry`  — JSON project/role registry and validation.
* :mod:`whilly.swarm.plan`      — structured plan contract and validation.
* :mod:`whilly.swarm.prompts`   — planner / worker / reviewer prompt builders.
* :mod:`whilly.swarm.agent`     — bounded agent subprocesses (process groups).
* :mod:`whilly.swarm.gitops`    — worktrees, SHAs and diffs.
* :mod:`whilly.swarm.store`     — swarm-specific PostgreSQL persistence.
* :mod:`whilly.swarm.runtime`   — conversation service and coordinator.

Tool restrictions on agents are Claude CLI permissions, not OS isolation.
"""
