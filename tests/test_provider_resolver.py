"""the ProviderResolver (§4.8): hints become one concrete route per run,
unsupported combinations fail before any model call, and resolving new
models never mints new "global" semaphores."""

from __future__ import annotations

import pytest

from dainframe.core.types import ExecutionHints
from dainframe.providers import (
    BaseAIProvider,
    HintResolutionError,
    ProviderTable,
)
from dainframe.providers.limits import ConcurrencyLimiter


class FakeProvider(BaseAIProvider):
    def __init__(self, model, limiter, flavor):
        self.model = model
        self.limiter = limiter
        self.flavor = flavor

    async def create_message(self, request):
        raise NotImplementedError

    async def is_available(self):
        return True


def builder(flavor):
    return lambda model, limiter: FakeProvider(model, limiter, flavor)


def table(**kwargs):
    defaults = dict(
        default_provider="anthropic",
        default_models={"anthropic": "claude-sonnet-5", "openai": "gpt-4o"},
        builders={"anthropic": builder("anthropic"), "openai": builder("openai")},
        effort_support={"anthropic": True, "openai": False},
    )
    defaults.update(kwargs)
    return ProviderTable(**defaults)


def test_empty_hints_resolve_to_the_default_route():
    resolved = table().resolve(ExecutionHints(), agent="aria")
    assert resolved.provider_name == "anthropic"
    assert resolved.model == "claude-sonnet-5"
    assert resolved.provider.model == "claude-sonnet-5"
    assert resolved.effort is None


def test_model_and_provider_hints_route():
    t = table()
    fast = t.resolve(ExecutionHints(model="claude-haiku-4-5"), agent="scorer")
    assert (fast.provider_name, fast.model) == ("anthropic", "claude-haiku-4-5")
    other = t.resolve(ExecutionHints(provider="openai"), agent="aria")
    assert (other.provider_name, other.model) == ("openai", "gpt-4o")
    assert other.provider.flavor == "openai"


def test_instances_are_cached_and_share_one_limiter():
    t = table()
    a = t.resolve(ExecutionHints(), agent="x")
    b = t.resolve(ExecutionHints(), agent="y")
    c = t.resolve(ExecutionHints(model="claude-haiku-4-5"), agent="z")
    assert a.provider is b.provider  # cached per (provider, model)
    assert a.provider is not c.provider
    assert a.provider.limiter is c.provider.limiter  # ONE semaphore, always


def test_unknown_provider_fails_before_any_call():
    with pytest.raises(HintResolutionError, match="unknown provider"):
        table().resolve(ExecutionHints(provider="mystery"), agent="aria")


def test_provider_without_default_model_needs_an_explicit_one():
    t = table(default_models={"anthropic": "claude-sonnet-5"})
    with pytest.raises(HintResolutionError, match="no model"):
        t.resolve(ExecutionHints(provider="openai"), agent="aria")
    # ...but an explicit model hint clears it
    r = t.resolve(ExecutionHints(provider="openai", model="gpt-4o"), agent="a")
    assert r.model == "gpt-4o"


def test_effort_on_an_unsupporting_provider_fails_loudly():
    """the openai adapter silently ignores effort it was never told about;
    an effort HINT routed there is the silent degradation §4.8 forbids."""
    with pytest.raises(HintResolutionError, match="cannot honor effort"):
        table().resolve(ExecutionHints(provider="openai", effort="low"), agent="scorer")


def test_unknown_effort_level_fails_loudly():
    with pytest.raises(HintResolutionError, match="unknown effort"):
        table().resolve(ExecutionHints(effort="maximal"), agent="aria")


def test_misconfigured_defaults_fail_at_construction():
    with pytest.raises(HintResolutionError, match="no builder"):
        ProviderTable(
            default_provider="mystery",
            default_models={"mystery": "m"},
            builders={"anthropic": builder("anthropic")},
        )
    with pytest.raises(HintResolutionError, match="no default model"):
        ProviderTable(
            default_provider="anthropic",
            default_models={},
            builders={"anthropic": builder("anthropic")},
        )


def test_shipped_builders_and_effort_map_are_the_defaults():
    t = ProviderTable(
        default_provider="anthropic",
        default_models={"anthropic": "claude-sonnet-5"},
    )
    assert set(t.builders) == {"anthropic", "openai"}
    assert t.effort_support == {"anthropic": True, "openai": False}
