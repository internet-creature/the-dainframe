"""the ProviderResolver (DESIGN.md §4.8): per-line model routing, made real.

an ExecutionHints on a ScriptLine is a REQUEST; the resolver turns it into
one concrete (provider, model, effort, max_tokens) for a WHOLE agent run -
never a passive decoration (§11.6). resolution happens once per run because
provider-native continuation blocks (thinking, tool blocks) cannot switch
provider halfway through.

unsupported combinations fail HERE, before the first model call - a hint
never silently degrades based on whichever SDK happens to be active. the
openai adapter ignoring an `effort` it was never told about is fine; an
`effort` HINT routed to a provider that cannot honor it is a
HintResolutionError.

`ProviderTable` is the shipped implementation: named provider builders
sharing ONE ConcurrencyLimiter, a default route, per-provider default
models, and cached instances per (provider, model) - resolving a new model
must never mint a new "global" semaphore (§4.8). model-tier subtleties
(anthropic utility models rejecting thinking/effort) are app policy: supply
a custom builder, or a custom resolver entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from dainframe.providers.base import BaseAIProvider
from dainframe.providers.limits import ConcurrencyLimiter

if TYPE_CHECKING:  # pragma: no cover - avoids core<->providers import cycle
    from dainframe.core.types import ExecutionHints


class HintResolutionError(ValueError):
    """an ExecutionHints combination no configured provider can honor,
    raised before any model call is made."""


@dataclass(frozen=True)
class ResolvedProvider:
    """one run's fixed routing: the provider instance to call, the name
    usage events carry, and the validated per-run dials."""

    provider: BaseAIProvider
    provider_name: str
    model: str
    effort: Optional[str] = None
    max_tokens: Optional[int] = None


@runtime_checkable
class ProviderResolver(Protocol):
    def resolve(self, hints: ExecutionHints, *, agent: str) -> ResolvedProvider: ...


# (model, shared limiter) -> a configured provider instance
ProviderBuilder = Callable[[str, ConcurrencyLimiter], BaseAIProvider]


def _build_anthropic(model: str, limiter: ConcurrencyLimiter) -> BaseAIProvider:
    from dainframe.providers.anthropic import AnthropicProvider

    return AnthropicProvider(model=model, limiter=limiter)


def _build_openai(model: str, limiter: ConcurrencyLimiter) -> BaseAIProvider:
    from dainframe.providers.openai import OpenAIProvider

    return OpenAIProvider(model=model, limiter=limiter)


_DEFAULT_BUILDERS: Mapping[str, ProviderBuilder] = {
    "anthropic": _build_anthropic,
    "openai": _build_openai,
}

# which shipped adapters can honor the `effort` dial. apps overriding
# builders can override this too (policy: "may this route carry effort").
_DEFAULT_EFFORT_SUPPORT: Mapping[str, bool] = {
    "anthropic": True,
    "openai": True,
}

# the levels each shipped adapter's current model generation accepts
# (capability: "which values are valid here"). anthropic's output_config
# has no "none" - omit the hint instead; openai's reasoning.effort does.
# apps routed to older models can narrow these via `effort_levels`.
_DEFAULT_EFFORT_LEVELS: Mapping[str, frozenset] = {
    "anthropic": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "openai": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
}


class ProviderTable:
    def __init__(
        self,
        *,
        default_provider: str,
        default_models: Mapping[str, str],
        limiter: Optional[ConcurrencyLimiter] = None,
        builders: Optional[Mapping[str, ProviderBuilder]] = None,
        effort_support: Optional[Mapping[str, bool]] = None,
        effort_levels: Optional[Mapping[str, frozenset]] = None,
    ):
        self.default_provider = default_provider
        self.default_models = dict(default_models)
        self.limiter = limiter or ConcurrencyLimiter()
        self.builders = (
            dict(builders) if builders is not None else dict(_DEFAULT_BUILDERS)
        )
        self.effort_support = (
            dict(effort_support)
            if effort_support is not None
            else dict(_DEFAULT_EFFORT_SUPPORT)
        )
        self.effort_levels = (
            dict(effort_levels)
            if effort_levels is not None
            else dict(_DEFAULT_EFFORT_LEVELS)
        )
        self._instances: dict[tuple[str, str], BaseAIProvider] = {}
        if default_provider not in self.builders:
            raise HintResolutionError(
                f"default provider '{default_provider}' has no builder"
            )
        if default_provider not in self.default_models:
            raise HintResolutionError(
                f"default provider '{default_provider}' has no default model"
            )

    def resolve(self, hints: ExecutionHints, *, agent: str) -> ResolvedProvider:
        name = hints.provider or self.default_provider
        if name not in self.builders:
            raise HintResolutionError(
                f"unknown provider '{name}' hinted for agent '{agent}'"
            )
        model = hints.model or self.default_models.get(name)
        if model is None:
            raise HintResolutionError(
                f"provider '{name}' hinted for agent '{agent}' with no model "
                "and no configured default model"
            )
        if hints.effort is not None:
            if not self.effort_support.get(name, False):
                raise HintResolutionError(
                    f"provider '{name}' cannot honor effort "
                    f"(hinted '{hints.effort}' for agent '{agent}'); route "
                    "the line to a provider that can, or drop the hint"
                )
            levels = self.effort_levels.get(name, frozenset())
            if hints.effort not in levels:
                raise HintResolutionError(
                    f"unknown effort '{hints.effort}' for provider '{name}' "
                    f"hinted for agent '{agent}' "
                    f"(expected one of {sorted(levels)})"
                )
        key = (name, model)
        if key not in self._instances:
            self._instances[key] = self.builders[name](model, self.limiter)
        return ResolvedProvider(
            provider=self._instances[key],
            provider_name=name,
            model=model,
            effort=hints.effort,
            max_tokens=hints.max_tokens,
        )
