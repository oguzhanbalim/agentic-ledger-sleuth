"""One conversation with Claude: a manual Messages API loop over client-side tools.

A manual loop rather than the beta tool runner because the harness needs four things at the boundary
of every turn: a branch on each stop reason, the same validated ``call_tool`` path the MCP server
uses, a hard turn cap, and an append-only history that can be continued with gate rejections.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, Literal, Protocol, TypeVar

from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from ledger_sleuth.tools import LedgerTools, ToolError, ToolSpec, call_tool

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# Thinking counts toward max_tokens. 16K leaves room and stays within the non-streaming limit.
DEFAULT_MAX_TOKENS = 16_000
DEFAULT_MAX_TURNS = 24
MAX_OUTPUT_REPAIRS = 2

OutputT = TypeVar("OutputT", bound=BaseModel)
LoopStatus = Literal["completed", "refused", "truncated", "turn_limit", "invalid_output", "error"]


class ClaudeClient(Protocol):
    """Anything with ``messages.create``: ``anthropic.Anthropic``, or a scripted fake in tests."""

    @property
    def messages(self) -> Any: ...


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            setattr(self, name, getattr(self, name) + (getattr(usage, name, 0) or 0))


@dataclass(slots=True)
class ToolCallRecord:
    name: str
    input: dict[str, Any]
    is_error: bool


@dataclass(slots=True)
class LoopResult(Generic[OutputT]):
    status: LoopStatus
    output: OutputT | None = None
    detail: str | None = None
    stop_reason: str | None = None


def tool_definitions(specs: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    """Strict tool definitions in a fixed order, so the cached prefix stays byte-identical.

    ``transform_schema`` moves constraints the API does not enforce into descriptions.
    ``call_tool`` still enforces them through the pydantic model.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": transform_schema(spec.input_model),
            "strict": True,
        }
        for spec in sorted(specs, key=lambda s: s.name)
    ]


class Conversation(Generic[OutputT]):
    """Append-only history for one role on one cluster.

    ``system`` and ``tools`` never change after construction and ``messages`` is only ever extended.
    Editing either would miss the cache and, on this model, invalidate replayed thinking blocks.
    """

    def __init__(
        self,
        client: ClaudeClient,
        *,
        system: str,
        tools: LedgerTools,
        specs: tuple[ToolSpec, ...],
        output_model: type[OutputT],
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_turns: int = DEFAULT_MAX_TURNS,
        usage: Usage | None = None,
    ) -> None:
        self._client = client
        self._tools = tools
        self._allowed = {spec.name for spec in specs}
        self._output_model = output_model
        self._max_turns = max_turns
        self._turns = 0
        self.usage = usage if usage is not None else Usage()
        self.messages: list[dict[str, Any]] = []
        self.tool_calls: list[ToolCallRecord] = []
        self._params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": transform_schema(output_model)},
            },
            # Explicit breakpoint on the frozen prefix (tools render before system),
            # automatic breakpoint on the growing conversation tail.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "cache_control": {"type": "ephemeral"},
            "tools": tool_definitions(specs),
        }

    def send(self, user_text: str) -> LoopResult[OutputT]:
        """Add a user message and run until the model finishes or the harness stops it."""
        self.messages.append({"role": "user", "content": user_text})
        return self._run()

    def _run(self) -> LoopResult[OutputT]:
        repairs = 0
        while self._turns < self._max_turns:
            self._turns += 1
            response = self._client.messages.create(**self._params, messages=self.messages)
            self.usage.add(response.usage)
            # Check the stop reason before reading content: a refusal may not match the schema.
            stop = response.stop_reason
            if stop == "refusal":
                return LoopResult(
                    "refused", detail=_refusal_detail(response), stop_reason="refusal"
                )
            if stop == "max_tokens":
                return LoopResult(
                    "truncated", detail="hit max_tokens before finishing", stop_reason=stop
                )
            # Thinking blocks go back unmodified, so the whole content list is appended as it came.
            self.messages.append({"role": "assistant", "content": response.content})
            if stop == "tool_use":
                self.messages.append({"role": "user", "content": self._run_tools(response)})
                continue
            if stop == "pause_turn":
                continue
            if stop != "end_turn":
                return LoopResult(
                    "error", detail=f"unexpected stop_reason {stop}", stop_reason=stop
                )
            try:
                return LoopResult("completed", output=self._parse(response), stop_reason=stop)
            except (ValidationError, ValueError) as exc:
                repairs += 1
                if repairs > MAX_OUTPUT_REPAIRS:
                    return LoopResult("invalid_output", detail=str(exc), stop_reason=stop)
                self.messages.append(
                    {
                        "role": "user",
                        "content": f"That output failed validation: {exc}. "
                        "Return only JSON matching the required schema.",
                    }
                )
        return LoopResult("turn_limit", detail=f"stopped after {self._max_turns} turns")

    def _run_tools(self, response: Any) -> list[dict[str, Any]]:
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content, is_error = self._execute(block.name, block.input)
            self.tool_calls.append(ToolCallRecord(block.name, dict(block.input), is_error))
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
            }
            if is_error:
                result["is_error"] = True
            results.append(result)
        return results

    def _execute(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        if name not in self._allowed:
            return f"Error: tool {name} is not available in this role.", True
        try:
            result = call_tool(self._tools, name, tool_input)
        except ToolError as exc:
            return f"Error: {exc}", True
        return json.dumps(result, sort_keys=True, default=str), False

    def _parse(self, response: Any) -> OutputT:
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise ValueError("response contained no text")
        return self._output_model.model_validate_json(text)


def _refusal_detail(response: Any) -> str:
    details = getattr(response, "stop_details", None)
    if details is None:
        return "refused without details"
    category = getattr(details, "category", None)
    explanation = getattr(details, "explanation", None)
    return f"category={category}; explanation={explanation}"
