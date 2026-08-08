"""OpenAIProvider request-kwargs tests.

mirrors test_anthropic_kwargs.py: locks down that effort reaches
reasoning.effort VERBATIM (the gpt-5.6 generation accepts the full
none/low/medium/high/xhigh/max ladder - per-route level validation is the
resolver's job, not the adapter's), and that no effort means no reasoning
key at all (the shape non-reasoning models require, same as the anthropic
utility tier).
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


def test_every_level_passes_through_verbatim():
    provider = _provider()
    for level in ("none", "low", "medium", "high", "xhigh", "max"):
        kwargs = provider._build_kwargs(_request(effort=level))
        assert kwargs["reasoning"] == {"effort": level}


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
