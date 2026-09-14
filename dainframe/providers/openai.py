import json
import logging
from typing import Optional

from openai import AsyncOpenAI, RateLimitError, AuthenticationError, APIError

from .base import BaseAIProvider
from .limits import ConcurrencyLimiter
from .types import (
    AIRequest,
    AIResponse,
    ChatTurn,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    ToolCall,
    Usage,
)

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseAIProvider):
    """openai provider (Responses API), adapted to the neutral interface.

    cache hints are anthropic concepts and are ignored here (openai
    prefix-caches automatically). `effort` is honored: it goes onto
    `reasoning.effort` VERBATIM - the gpt-5.6 generation accepts the full
    none/low/medium/high/xhigh/max ladder, and a level an older model
    rejects should fail loudly at the API, never silently degrade
    (validating levels per route is the resolver's job). effort is only
    sent when the request supplies it - non-reasoning models must be
    paired with effort=None requests, same as the anthropic utility tier.

    tools are rendered in STRICT mode by default (`strict_tools=True`): the
    neutral ToolDef schema is rewritten into openai's strict form (every
    property required, the optional ones nullable, no additional
    properties) so the model is grammar-constrained to the schema and says
    `null` for a field it isn't setting, instead of guessing a placeholder
    ("", 0, the first enum value) - which is what a padded call looks like
    on the wire and what a handler would then apply as a real value. the
    nulls are stripped from the parsed arguments before the ToolCall is
    built, so the neutral contract holds: `ToolCall.input` carries only the
    fields the model chose to set, whichever provider produced it. the
    anthropic side already meets that contract natively and is untouched.
    `strict_tools=False` sends the ToolDef schema verbatim (the escape hatch
    for a consumer whose schema uses a keyword strict mode rejects).
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        limiter: Optional[ConcurrencyLimiter] = None,
        strict_tools: bool = True,
    ):
        self.model = model
        self.strict_tools = strict_tools
        # api_key=None falls back to the sdk's OPENAI_API_KEY env lookup
        self.client = AsyncOpenAI(api_key=api_key)
        # inject ONE limiter across providers that should share the in-flight
        # ceiling; a provider constructed without one gets a private default.
        self.limiter = limiter or ConcurrencyLimiter()

    def _build_kwargs(self, request: AIRequest) -> dict:
        kwargs = {
            "model": self.model,
            "instructions": "\n\n".join(b.text for b in request.system),
            "input": self._render_input(request.messages),
            "max_output_tokens": request.max_tokens,
        }
        if request.effort:
            kwargs["reasoning"] = {"effort": request.effort}
        if request.tools:
            kwargs["tools"] = self._render_tools(request.tools)
        return kwargs

    async def create_message(self, request: AIRequest) -> AIResponse:
        kwargs = self._build_kwargs(request)

        try:
            async with self.limiter:
                response = await self.client.responses.create(**kwargs)
        except RateLimitError as e:
            # 429 is most commonly out of quota/funds, not true rate limiting
            raise ProviderRateLimited(str(e)) from e
        except AuthenticationError as e:
            raise ProviderUnavailable(str(e)) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

        return self._normalize(response)

    # --- rendering ---------------------------------------------------------

    def _render_tools(self, tools) -> list[dict]:
        rendered = []
        for t in tools:
            item = {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": (
                    strict_schema(t.input_schema)
                    if self.strict_tools
                    else t.input_schema
                ),
            }
            if self.strict_tools:
                item["strict"] = True
            rendered.append(item)
        return rendered

    def _render_input(self, messages: list[ChatTurn]) -> list[dict]:
        items: list[dict] = []
        for turn in messages:
            # assistant continuation: re-send the function_call items verbatim
            if turn.provider_blocks is not None:
                items.extend(turn.provider_blocks)
                continue

            if turn.tool_results:
                for r in turn.tool_results:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": r.tool_call_id,
                            "output": r.content,
                        }
                    )
                continue

            items.append({"role": turn.role, "content": turn.content or ""})
        return items

    # --- normalization -----------------------------------------------------

    def _normalize(self, response) -> AIResponse:
        text_parts = []
        tool_calls = []
        echo_blocks = []  # function_call items to re-send on continuation

        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) in ("output_text", "text"):
                        text_parts.append(getattr(part, "text", ""))
            elif item_type == "function_call":
                call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
                name = getattr(item, "name", "")
                raw_args = getattr(item, "arguments", "") or "{}"
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed_args = {}
                # strict mode makes the model say null for every field it
                # isn't setting; the neutral ToolCall carries absence as
                # absence. the raw arguments are echoed back verbatim below.
                tool_calls.append(
                    ToolCall(id=call_id, name=name, input=without_nulls(parsed_args))
                )
                echo_blocks.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": raw_args,
                    }
                )

        text = "".join(text_parts).strip() or None

        u = getattr(response, "usage", None)
        cached = 0
        if u is not None:
            details = getattr(u, "input_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0 if u else 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0 if u else 0,
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )

        stop_reason = "tool_use" if tool_calls else "end_turn"
        assistant_turn = ChatTurn(
            role="assistant",
            content=text,
            tool_calls=tool_calls or None,
            provider_blocks=echo_blocks or None,
        )

        return AIResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            assistant_turn=assistant_turn,
            model=self.model,
        )

    async def is_available(self) -> bool:
        if not self.client.api_key:
            return False
        try:
            await self.client.models.retrieve(self.model)
            return True
        except Exception as e:
            logger.warning("openai provider configured but unavailable: %s", e)
            return False


# --- strict-mode schema rendering ----------------------------------------


def strict_schema(schema: dict) -> dict:
    """a copy of a JSON Schema in openai strict form.

    strict mode's two rules, applied to every object at every depth: all
    properties listed in `required`, and `additionalProperties: false`. a
    property the source schema left optional becomes nullable (its type
    gains "null"; an enum gains None) so the model can still leave it
    alone - by saying null, which `without_nulls` erases on the way back.
    the source schema is never mutated (ToolDefs are shared, and the
    anthropic provider sends them verbatim). the output is deterministic,
    so the rendered tool bytes are stable across requests (openai caches
    the processed schema per distinct shape).
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    props = schema.get("properties")
    is_object = schema.get("type") == "object" or props is not None
    if is_object:
        props = props or {}
        required = set(schema.get("required") or ())
        out["properties"] = {
            name: (
                strict_schema(sub)
                if name in required
                else _nullable(strict_schema(sub))
            )
            for name, sub in props.items()
        }
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    if "items" in schema:
        out["items"] = strict_schema(schema["items"])
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            out[combinator] = [strict_schema(s) for s in schema[combinator]]
    if "$defs" in schema:
        out["$defs"] = {k: strict_schema(v) for k, v in schema["$defs"].items()}
    return out


def _nullable(schema: dict) -> dict:
    """the same schema, also accepting null."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    t = schema.get("type")
    if t is None:
        # no bare type to widen (a combinator, or an untyped schema): wrap
        if "anyOf" in schema:
            branches = list(schema["anyOf"])
            if not any(b.get("type") == "null" for b in branches):
                branches.append({"type": "null"})
            out["anyOf"] = branches
            return out
        return {"anyOf": [out, {"type": "null"}]}
    types = list(t) if isinstance(t, list) else [t]
    if "null" not in types:
        types.append("null")
    out["type"] = types
    if "enum" in schema and None not in schema["enum"]:
        out["enum"] = list(schema["enum"]) + [None]
    return out


def without_nulls(value):
    """the same JSON value with every null-valued object key removed, at
    every depth. list elements are kept (null there is positional)."""
    if isinstance(value, dict):
        return {k: without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_nulls(v) for v in value]
    return value
