"""Validated execution profiles for planner and worker engine selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from whilly.swarm.registry import EngineConfig, Registry, RegistryError

SUPPORTED_EFFORTS = {"low", "medium", "high"}
PROFILE_KEYS = {"engine", "model", "reasoning_effort", "max_turns", "timeout_seconds", "budget_usd"}


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    engine: str
    model: str
    reasoning_effort: str
    max_turns: int
    timeout_seconds: int
    budget_usd: float | None


def parse_profile(
    name: str, raw: Any, engines: dict[str, EngineConfig], problems: list[str]
) -> ExecutionProfile | None:
    where = f"profiles.{name}"
    initial_problems = len(problems)
    if not isinstance(raw, dict):
        problems.append(f"{where} must be an object")
        return None
    unknown = set(raw) - PROFILE_KEYS
    if unknown:
        problems.append(f"{where}: unknown keys {sorted(unknown)}")
    engine = raw.get("engine")
    model = raw.get("model")
    effort = raw.get("reasoning_effort")
    max_turns = raw.get("max_turns")
    timeout = raw.get("timeout_seconds")
    budget = raw.get("budget_usd")
    if engine not in engines:
        problems.append(f"{where}.engine must name a configured engine")
    if not isinstance(model, str) or not model.strip():
        problems.append(f"{where}.model must be an explicit non-empty string")
    if effort not in SUPPORTED_EFFORTS:
        problems.append(f"{where}.reasoning_effort must be one of {sorted(SUPPORTED_EFFORTS)}")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        problems.append(f"{where}.max_turns must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        problems.append(f"{where}.timeout_seconds must be a positive integer")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0
    ):
        problems.append(f"{where}.budget_usd must be null or a positive finite number")
    if len(problems) != initial_problems:
        return None
    return ExecutionProfile(
        name, engine, model.strip(), effort, max_turns, timeout, None if budget is None else float(budget)
    )


def resolve_profile(registry: Registry, name: str) -> ExecutionProfile:
    try:
        return registry.profiles[name]
    except KeyError as exc:
        raise RegistryError([f"unknown profile {name!r}; no fallback is permitted"]) from exc


def profile_engine(registry: Registry, name: str, *, worker: bool = False) -> EngineConfig:
    profile = resolve_profile(registry, name)
    if worker and name.startswith("planner-"):
        raise RegistryError([f"worker cannot select planner profile {name!r}"])
    base = registry.engines[profile.engine]
    return EngineConfig(base.executable, profile.model, base.worker_args, base.inherit_mcp, profile.reasoning_effort)
