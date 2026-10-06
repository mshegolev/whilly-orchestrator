from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest

from whilly.swarm.engines import build_engine_argv
from whilly.swarm.profiles import profile_engine, resolve_profile
from whilly.swarm.registry import EngineConfig, RegistryError, registry_from_dict


def _registry(**overrides):
    data = {
        "version": 1,
        "name": "demo",
        "projects": {"demo": {"path": ".", "purpose": "demo"}},
        "roles": {"worker": {"purpose": "work", "projects": ["demo"]}},
        "engines": {
            "claude": {"executable": ["claude"], "model": "cheap-model"},
            "codex": {"executable": ["codex"], "model": "codex-model"},
        },
    }
    data.update(overrides)
    return registry_from_dict(data, check_git=False)


def test_legacy_registry_roundtrip_and_codex_reasoning_default_is_low():
    registry = _registry(engines=None)
    assert registry.profiles == {}
    assert registry.engines["claude"].model == "claude-haiku"
    assert (
        build_engine_argv("codex", EngineConfig(("codex",), "model"), mode="worker")[6]
        == 'model_reasoning_effort="low"'
    )


def test_profiles_resolve_distinct_strong_and_cheap_models():
    registry = _registry(
        profiles={
            "planner-strong": {
                "engine": "codex",
                "model": "strong",
                "reasoning_effort": "high",
                "max_turns": 8,
                "timeout_seconds": 60,
                "budget_usd": 2,
            },
            "worker-cheap": {
                "engine": "codex",
                "model": "cheap",
                "reasoning_effort": "low",
                "max_turns": 4,
                "timeout_seconds": 30,
                "budget_usd": 0.5,
            },
        }
    )
    assert resolve_profile(registry, "planner-strong").model != resolve_profile(registry, "worker-cheap").model
    assert profile_engine(registry, "planner-strong").reasoning_effort == "high"


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_subscription_profile_can_omit_money_limit_without_removing_runtime_limits(engine, explicit_null):
    profile = {
        "engine": engine,
        "model": "selected-model",
        "reasoning_effort": "medium",
        "max_turns": 8,
        "timeout_seconds": 120,
    }
    if explicit_null:
        profile["budget_usd"] = None
    registry = _registry(profiles={"planner-strong": profile})
    selected = resolve_profile(registry, "planner-strong")
    assert selected.budget_usd is None
    assert selected.max_turns == 8 and selected.timeout_seconds == 120
    argv = build_engine_argv(
        engine,
        profile_engine(registry, "planner-strong"),
        mode="read_only",
        max_turns=selected.max_turns,
        budget_usd=selected.budget_usd,
    )
    assert "--max-budget-usd" not in argv
    assert argv[argv.index("--model") + 1] == "selected-model"


@pytest.mark.parametrize("budget", [0, -1, True, "1", math.nan, math.inf])
def test_optional_money_limit_still_rejects_invalid_explicit_values(budget):
    with pytest.raises(RegistryError, match="budget_usd"):
        _registry(
            profiles={
                "planner-strong": {
                    "engine": "codex",
                    "model": "strong",
                    "reasoning_effort": "medium",
                    "max_turns": 8,
                    "timeout_seconds": 120,
                    "budget_usd": budget,
                }
            }
        )


@pytest.mark.parametrize("field", ["planner_engine", "review_engine"])
@pytest.mark.parametrize("engine", ["missing", None, [], True])
def test_agent_engine_selection_is_validated_before_execution(field, engine):
    with pytest.raises(RegistryError, match=field):
        _registry(agent={field: engine})


def test_same_engine_profiles_keep_distinct_models_in_actual_argv():
    registry = _registry(
        profiles={
            "worker-cheap": {
                "engine": "codex",
                "model": "worker-model",
                "reasoning_effort": "low",
                "max_turns": 4,
                "timeout_seconds": 30,
                "budget_usd": 0.5,
            },
            "reviewer-cheap": {
                "engine": "codex",
                "model": "reviewer-model",
                "reasoning_effort": "medium",
                "max_turns": 5,
                "timeout_seconds": 40,
                "budget_usd": 0.6,
            },
        }
    )
    worker_argv = build_engine_argv("codex", profile_engine(registry, "worker-cheap"), mode="worker")
    reviewer_argv = build_engine_argv("codex", profile_engine(registry, "reviewer-cheap"), mode="read_only")
    assert worker_argv[worker_argv.index("--model") + 1] == "worker-model"
    assert reviewer_argv[reviewer_argv.index("--model") + 1] == "reviewer-model"
    assert 'model_reasoning_effort="low"' in worker_argv
    assert 'model_reasoning_effort="medium"' in reviewer_argv


def test_mixed_engine_worker_variants_have_explicit_actual_argv():
    registry = _registry(
        profiles={
            "worker-cheap": {
                "engine": "claude",
                "model": "cheap-claude",
                "reasoning_effort": "low",
                "max_turns": 3,
                "timeout_seconds": 20,
                "budget_usd": 0.4,
            },
            "worker-cheap-codex": {
                "engine": "codex",
                "model": "cheap-codex",
                "reasoning_effort": "low",
                "max_turns": 4,
                "timeout_seconds": 25,
                "budget_usd": 0.5,
            },
        }
    )
    claude = build_engine_argv("claude", profile_engine(registry, "worker-cheap"), mode="worker")
    codex = build_engine_argv("codex", profile_engine(registry, "worker-cheap-codex"), mode="worker")
    assert claude[claude.index("--model") + 1] == "cheap-claude"
    assert codex[codex.index("--model") + 1] == "cheap-codex"


@pytest.mark.parametrize(
    "profiles",
    [
        {
            "x": {
                "engine": "missing",
                "model": "m",
                "reasoning_effort": "low",
                "max_turns": 1,
                "timeout_seconds": 1,
                "budget_usd": 1,
            }
        },
        {
            "x": {
                "engine": "codex",
                "model": " ",
                "reasoning_effort": "low",
                "max_turns": 1,
                "timeout_seconds": 1,
                "budget_usd": 1,
            }
        },
        {
            "x": {
                "engine": "codex",
                "model": "m",
                "reasoning_effort": "extreme",
                "max_turns": 1,
                "timeout_seconds": 1,
                "budget_usd": 1,
            }
        },
        {
            "x": {
                "engine": "codex",
                "model": "m",
                "reasoning_effort": "low",
                "max_turns": 0,
                "timeout_seconds": 1,
                "budget_usd": math.inf,
            }
        },
    ],
)
def test_profiles_reject_invalid_values(profiles):
    with pytest.raises(RegistryError):
        _registry(profiles=profiles)


def test_unknown_profile_and_planner_profile_for_worker_never_fallback():
    registry = _registry(
        profiles={
            "planner-strong": {
                "engine": "codex",
                "model": "strong",
                "reasoning_effort": "high",
                "max_turns": 8,
                "timeout_seconds": 60,
                "budget_usd": 2,
            }
        }
    )
    with pytest.raises(RegistryError, match="unknown profile"):
        resolve_profile(registry, "missing")
    with pytest.raises(RegistryError, match="worker cannot select planner"):
        profile_engine(registry, "planner-strong", worker=True)


async def test_feature_default_engine_is_worker_not_planner():
    from types import SimpleNamespace
    from whilly.swarm.runtime import Coordinator

    registry = _registry(
        profiles={
            "worker-cheap": {
                "engine": "codex",
                "model": "cheap",
                "reasoning_effort": "low",
                "max_turns": 4,
                "timeout_seconds": 30,
                "budget_usd": 1,
            }
        }
    )
    coordinator = object.__new__(Coordinator)
    coordinator.registry = registry
    coordinator.session_id = "test"
    coordinator.store = SimpleNamespace(get_revision=AsyncMock(return_value={"plan": {"tasks": []}}))
    coordinator._feature_worker_config = profile_engine(registry, "worker-cheap")
    assert await coordinator._task_engine({"revision": 1, "local_id": "task", "role": "worker"}) == "codex"
