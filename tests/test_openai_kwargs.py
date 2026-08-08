"""OpenAIProvider request-kwargs tests.

mirrors test_anthropic_kwargs.py: locks down the effort -> reasoning.effort
mapping. levels shared with openai pass through, anthropic-only levels
collapse to "high", and no effort means no reasoning key at all (the shape
non-reasoning models require, same as the anthropic utility tier).
"""

from dainframe.providers.openai import OpenAIProvider
from dainframe.providers.types import AIRequest, ChatTurn, SystemBlock, ToolDef


def _request(effort=None, tools=None):
    return AIRequest(
        system=[SystemBlock(text="persona")],
        messages=[ChatTurn(role="user", content="hi")],
        tools=tools or [],
        max_tokens=512,
        effort=effort,
    )


def _provider():
    return OpenAIProvider(model="gpt-5.6-terra", api_key="x")


def test_effort_maps_to_reasoning():
    kwargs = _provider()._build_kwargs(_request(effort="low"))
    assert kwargs["reasoning"] == {"effort": "low"}


def test_anthropic_only_levels_collapse_to_high():
    provider = _provider()
    assert provider._build_kwargs(_request(effort="xhigh"))["reasoning"] == {"effort": "high"}
    assert provider._build_kwargs(_request(effort="max"))["reasoning"] == {"effort": "high"}


def test_unknown_level_passes_through():
    # future openai-native levels shouldn't need a dainframe release
    kwargs = _provider()._build_kwargs(_request(effort="minimal"))
    assert kwargs["reasoning"] == {"effort": "minimal"}


def test_no_effort_omits_reasoning():
    kwargs = _provider()._build_kwargs(_request(effort=None))
    assert "reasoning" not in kwargs
    # the essentials are still there
    assert kwargs["model"] == "gpt-5.6-terra"
    assert kwargs["max_output_tokens"] == 512
    assert kwargs["instructions"] == "persona"
    assert kwargs["input"]


def test_tools_included_when_present():
    tools = [ToolDef(name="t", description="d", input_schema={"type": "object"})]
    kwargs = _provider()._build_kwargs(_request(tools=tools))
    assert kwargs["tools"][0]["name"] == "t"
    assert kwargs["tools"][0]["type"] == "function"
