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


# --- strict tool rendering ------------------------------------------------
#
# the padded-call fix (chordial's "archive the pomodoro tasks" morning):
# gpt-5.6-terra filled every optional field it wasn't setting with "" / 0 /
# the first enum value, and the handlers applied those as real values.
# strict mode makes the model say null instead, and the provider erases
# the nulls before the neutral ToolCall is built.

from types import SimpleNamespace

from dainframe.providers.openai import prune_introduced_nulls, strict_schema


SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Title or id."},
        "status": {"type": "string", "enum": ["To do", "Done"]},
        "pom_estimate": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "thing": {
            "type": "object",
            "description": "exactly one of",
            "properties": {"task_id": {"type": "integer"}, "label": {"type": "string"}},
        },
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"line": {"type": "string"}, "why": {"type": "string"}},
                "required": ["line"],
            },
            "minItems": 3,
        },
    },
    "required": ["task"],
}


def test_strict_rendering_requires_every_field_and_makes_optionals_nullable():
    tools = [ToolDef(name="update_task", description="d", input_schema=SCHEMA)]
    rendered = _provider()._build_kwargs(_request(tools=tools))["tools"][0]
    assert rendered["strict"] is True
    params = rendered["parameters"]
    assert params["required"] == list(SCHEMA["properties"])
    assert params["additionalProperties"] is False
    props = params["properties"]
    # required stays exactly as declared
    assert props["task"] == {"type": "string", "description": "Title or id."}
    # optionals widen to accept null; an enum gains None as a member
    assert props["status"]["type"] == ["string", "null"]
    assert props["status"]["enum"] == ["To do", "Done", None]
    assert props["pom_estimate"]["type"] == ["number", "null"]
    assert props["tags"]["type"] == ["array", "null"]
    assert props["tags"]["items"] == {"type": "string"}


def test_strict_rendering_recurses_into_nested_objects_and_array_items():
    params = strict_schema(SCHEMA)
    thing = params["properties"]["thing"]
    assert thing["type"] == ["object", "null"]
    assert thing["description"] == "exactly one of"
    assert thing["required"] == ["task_id", "label"]
    assert thing["additionalProperties"] is False
    assert thing["properties"]["task_id"]["type"] == ["integer", "null"]

    item = params["properties"]["proposals"]["items"]
    assert item["required"] == ["line", "why"]
    assert item["additionalProperties"] is False
    assert item["properties"]["line"] == {"type": "string"}
    assert item["properties"]["why"]["type"] == ["string", "null"]
    # unrelated keywords ride through untouched
    assert params["properties"]["proposals"]["minItems"] == 3


def test_strict_rendering_never_mutates_the_shared_tooldef():
    import copy

    before = copy.deepcopy(SCHEMA)
    strict_schema(SCHEMA)
    assert SCHEMA == before


def test_strict_rendering_is_deterministic():
    assert strict_schema(SCHEMA) == strict_schema(SCHEMA)


def test_bare_object_schema_is_a_valid_strict_object():
    assert strict_schema({"type": "object"}) == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_strict_tools_off_sends_the_schema_verbatim():
    provider = OpenAIProvider(model="gpt-5.6-terra", api_key="x", strict_tools=False)
    tools = [ToolDef(name="t", description="d", input_schema=SCHEMA)]
    rendered = provider._build_kwargs(_request(tools=tools))["tools"][0]
    assert "strict" not in rendered
    assert rendered["parameters"] is SCHEMA


def test_pruning_erases_only_the_nulls_strict_mode_introduced():
    padded = {
        "task": "Pomodoro 2",
        "status": "deprioritized",
        "project": None,
        "thing": {"task_id": 4, "label": None},
        "proposals": [{"line": "a", "why": None}],
        "tags": [None, "x"],
        "unknown": None,
    }
    assert prune_introduced_nulls(padded, SCHEMA) == {
        "task": "Pomodoro 2",
        "status": "deprioritized",
        "thing": {"task_id": 4},
        "proposals": [{"line": "a"}],
        "tags": [None, "x"],
    }


NULLABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": ["string", "null"]},
        "note": {"type": "string"},
        "inner": {
            "type": "object",
            "properties": {"reset": {"type": ["boolean", "null"]}},
            "required": ["reset"],
        },
    },
    "required": ["value", "inner"],
}


def test_pruning_keeps_an_explicit_null_on_a_required_nullable_field():
    # the source schema asked for it and allowed null: that null is a value,
    # the same thing the anthropic provider hands through
    args = {"value": None, "note": None, "inner": {"reset": None}}
    assert prune_introduced_nulls(args, NULLABLE_SCHEMA) == {
        "value": None,
        "inner": {"reset": None},
    }
    # and strict rendering leaves those declarations alone
    props = strict_schema(NULLABLE_SCHEMA)["properties"]
    assert props["value"]["type"] == ["string", "null"]
    assert props["note"]["type"] == ["string", "null"]


def _response(arguments: str):
    call = SimpleNamespace(
        type="function_call", call_id="c1", name="update_task", arguments=arguments
    )
    return SimpleNamespace(output=[call], usage=None)


def test_create_message_path_prunes_by_the_request_schemas_and_echoes_raw():
    raw = '{"task": "Pomodoro 2", "status": "deprioritized", "project": null}'
    provider = _provider()
    tools = [ToolDef(name="update_task", description="d", input_schema=SCHEMA)]
    schemas = provider._source_schemas(_request(tools=tools))
    result = provider._normalize(_response(raw), schemas)
    [call] = result.tool_calls
    assert call.input == {"task": "Pomodoro 2", "status": "deprioritized"}
    # the continuation re-sends what the model actually said
    assert result.assistant_turn.provider_blocks[0]["arguments"] == raw


def test_strict_off_keeps_every_null_verbatim():
    provider = OpenAIProvider(model="gpt-5.6-terra", api_key="x", strict_tools=False)
    tools = [ToolDef(name="update_task", description="d", input_schema=SCHEMA)]
    assert provider._source_schemas(_request(tools=tools)) == {}
    raw = '{"task": "Pomodoro 2", "project": null}'
    [call] = provider._normalize(_response(raw), {}).tool_calls
    assert call.input == {"task": "Pomodoro 2", "project": None}


def test_a_tool_the_request_did_not_declare_is_not_pruned():
    raw = '{"task": "Pomodoro 2", "project": null}'
    [call] = _provider()._normalize(_response(raw), {"other": SCHEMA}).tool_calls
    assert call.input == {"task": "Pomodoro 2", "project": None}
