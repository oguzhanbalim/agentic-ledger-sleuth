from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict

from helpers import THINKING, ScriptedClient, message, text, tool_use
from ledger_sleuth.agent.loop import Conversation, tool_definitions
from ledger_sleuth.agent.orchestrator import READ_ONLY_SPECS
from ledger_sleuth.tools import TOOL_SPECS, LedgerTools


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


def _conversation(client: Any, tools: LedgerTools, **kwargs: Any) -> Conversation[Answer]:
    return Conversation(
        client,
        system="Stable system prompt.",
        tools=tools,
        specs=TOOL_SPECS,
        output_model=Answer,
        **kwargs,
    )


def _user_tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    return list(request["messages"][-1]["content"])


def test_request_follows_the_api_rules_for_this_model(tools: LedgerTools) -> None:
    client = ScriptedClient([message([text({"answer": "ok"})])])
    _conversation(client, tools).send("Go.")
    request = client.messages.requests[0]

    assert request["model"] == "claude-opus-5-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in request, "forced tool_choice returns a 400 on this model"
    for removed in ("temperature", "top_p", "top_k"):
        assert removed not in request
    assert "budget_tokens" not in json.dumps(request["thinking"])
    assert request["messages"][-1]["role"] == "user", "no prefill"
    assert request["max_tokens"] <= 21_333, "non-streaming calls above this raise in the SDK"


def test_prompt_caching_is_on_the_stable_prefix(tools: LedgerTools) -> None:
    client = ScriptedClient([message([text({"answer": "ok"})])])
    _conversation(client, tools).send("Go.")
    request = client.messages.requests[0]
    assert request["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request["cache_control"] == {"type": "ephemeral"}


def test_tools_are_strict_sorted_and_closed() -> None:
    definitions = tool_definitions(TOOL_SPECS)
    assert [d["name"] for d in definitions] == sorted(d["name"] for d in definitions)
    for definition in definitions:
        assert definition["strict"] is True
        assert definition["input_schema"]["additionalProperties"] is False
        rendered = json.dumps(definition["input_schema"])
        assert '"minimum"' not in rendered
        assert '"maximum"' not in rendered


def test_tool_call_runs_and_history_stays_append_only(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [
            message(
                [THINKING, tool_use("get_approval_matrix", {"company_id": "CO-A"})],
                stop_reason="tool_use",
            ),
            message([THINKING, text({"answer": "done"})]),
        ]
    )
    conversation = _conversation(client, tools)
    result = conversation.send("Go.")
    assert result.status == "completed"
    assert result.output == Answer(answer="done")

    first, second = client.messages.requests
    assert json.dumps(first["system"]) == json.dumps(second["system"])
    assert json.dumps(first["tools"]) == json.dumps(second["tools"])
    assert second["messages"][0] == first["messages"][0]

    results = _user_tool_results(second)
    assert results[0]["tool_use_id"] == "toolu_1"
    assert "is_error" not in results[0]
    assert json.loads(results[0]["content"])["tiers"][0]["company_id"] == "CO-A"


def test_thinking_blocks_return_unmodified(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [
            message(
                [THINKING, tool_use("run_matcher", {})],
                stop_reason="tool_use",
            ),
            message([text({"answer": "done"})]),
        ]
    )
    _conversation(client, tools).send("Go.")
    replayed = client.messages.requests[1]["messages"][1]
    assert replayed["role"] == "assistant"
    assert replayed["content"][0].type == "thinking"
    assert replayed["content"][0].signature == "sig-abc"


def test_all_parallel_tool_calls_answered_in_one_message(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [
            message(
                [
                    tool_use("run_matcher", {}, "toolu_a"),
                    tool_use("get_approval_matrix", {}, "toolu_b"),
                ],
                stop_reason="tool_use",
            ),
            message([text({"answer": "done"})]),
        ]
    )
    _conversation(client, tools).send("Go.")
    ids = [r["tool_use_id"] for r in _user_tool_results(client.messages.requests[1])]
    assert ids == ["toolu_a", "toolu_b"]


def test_invalid_tool_input_returns_is_error_and_loop_continues(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [
            message([tool_use("get_ledger_rows", {"limit": -5})], stop_reason="tool_use"),
            message([text({"answer": "recovered"})]),
        ]
    )
    result = _conversation(client, tools).send("Go.")
    assert result.status == "completed"
    tool_result = _user_tool_results(client.messages.requests[1])[0]
    assert tool_result["is_error"] is True
    assert "invalid input" in tool_result["content"]


def test_tool_outside_the_role_is_refused_by_the_harness(tools: LedgerTools) -> None:
    row = tools.store.ledger_rows[0]
    client = ScriptedClient(
        [
            message(
                [
                    tool_use(
                        "propose_adjustment",
                        {
                            "target_row_id": row.row_id,
                            "adjustment_type": "hold_payment",
                            "amount_minor": row.amount_minor,
                            "currency": row.currency,
                            "reason": "Not allowed in this role.",
                            "evidence_row_ids": [row.row_id],
                        },
                    )
                ],
                stop_reason="tool_use",
            ),
            message([text({"answer": "ok"})]),
        ]
    )
    conversation: Conversation[Answer] = Conversation(
        client, system="S", tools=tools, specs=READ_ONLY_SPECS, output_model=Answer
    )
    conversation.send("Go.")
    tool_result = _user_tool_results(client.messages.requests[1])[0]
    assert tool_result["is_error"] is True
    assert "not available in this role" in tool_result["content"]
    assert tools.queue.all_drafts() == []
    assert "propose_adjustment" not in {d["name"] for d in client.messages.requests[0]["tools"]}


def test_refusal_stops_without_reading_content(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [message([], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"})]
    )
    result = _conversation(client, tools).send("Go.")
    assert result.status == "refused"
    assert result.output is None
    assert "cyber" in (result.detail or "")
    assert client.messages.script == []


def test_max_tokens_is_reported_not_parsed(tools: LedgerTools) -> None:
    client = ScriptedClient([message([text('{"answer": "trunc')], stop_reason="max_tokens")])
    result = _conversation(client, tools).send("Go.")
    assert result.status == "truncated"
    assert result.output is None


def test_pause_turn_resends_and_continues(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [
            message([THINKING], stop_reason="pause_turn"),
            message([text({"answer": "resumed"})]),
        ]
    )
    result = _conversation(client, tools).send("Go.")
    assert result.status == "completed"
    assert client.messages.requests[1]["messages"][-1]["role"] == "assistant"


def test_unknown_stop_reason_is_an_error(tools: LedgerTools) -> None:
    client = ScriptedClient([message([text("x")], stop_reason="stop_sequence")])
    assert _conversation(client, tools).send("Go.").status == "error"


def test_turn_limit_ends_the_loop(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [message([tool_use("run_matcher", {})], stop_reason="tool_use") for _ in range(5)]
    )
    result = _conversation(client, tools, max_turns=3).send("Go.")
    assert result.status == "turn_limit"
    assert len(client.messages.requests) == 3


def test_malformed_output_is_repaired_then_given_up_on(tools: LedgerTools) -> None:
    client = ScriptedClient([message([text("not json")]), message([text({"answer": "fixed"})])])
    assert _conversation(client, tools).send("Go.").output == Answer(answer="fixed")

    client = ScriptedClient([message([text("still not json")]) for _ in range(3)])
    assert _conversation(client, tools).send("Go.").status == "invalid_output"


def test_usage_accumulates(tools: LedgerTools) -> None:
    client = ScriptedClient(
        [
            message([tool_use("run_matcher", {})], stop_reason="tool_use"),
            message([text({"answer": "done"})]),
        ]
    )
    conversation = _conversation(client, tools)
    conversation.send("Go.")
    assert conversation.usage.requests == 2
    assert conversation.usage.input_tokens == 20
    assert conversation.usage.cache_read_input_tokens == 6


def test_real_sdk_client_serialises_and_parses_this_exact_request(tools: LedgerTools) -> None:
    """Runs the genuine SDK over a mock HTTP transport: nothing leaves the process."""
    bodies: list[dict[str, Any]] = []
    responses = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {"type": "tool_use", "id": "toolu_1", "name": "run_matcher", "input": {}},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "content": [{"type": "text", "text": '{"answer": "via sdk"}'}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages"
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=responses[len(bodies) - 1])

    client = anthropic.Anthropic(
        api_key="test-key-not-used",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    result = _conversation(client, tools).send("Go.")
    assert result.output == Answer(answer="via sdk")

    first, second = bodies
    assert first["thinking"]["type"] == "adaptive"
    assert first["output_config"]["effort"] == "high"
    assert first["tools"][0]["strict"] is True
    assert "tool_choice" not in first
    assert second["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig-1",
    }
    assert second["messages"][2]["content"][0]["type"] == "tool_result"


@pytest.mark.parametrize("name", ["investigator", "reviewer"])
def test_system_prompts_hold_nothing_volatile(name: str) -> None:
    from ledger_sleuth.agent import prompts
    from ledger_sleuth.agent.skills import load_skills

    build = getattr(prompts, f"{name}_system")
    assert build(load_skills()) == build(load_skills())
