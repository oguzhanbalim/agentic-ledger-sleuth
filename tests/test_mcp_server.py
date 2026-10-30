from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from ledger_sleuth.generator import TRAIN_SEED
from ledger_sleuth.mcp_server import build_server
from ledger_sleuth.store import LedgerStore
from ledger_sleuth.tools import SPECS_BY_NAME, LedgerTools, call_tool


def _payload(result: Any) -> dict[str, Any]:
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


def test_lists_every_tool_with_correct_read_only_hint(tools: LedgerTools) -> None:
    async def run() -> Any:
        async with Client(build_server(tools)) as client:
            return await client.list_tools()

    listed = asyncio.run(run()).tools
    assert {t.name for t in listed} == set(SPECS_BY_NAME)
    for tool in listed:
        assert tool.annotations.read_only_hint is SPECS_BY_NAME[tool.name].read_only
        assert tool.annotations.destructive_hint is False


def test_same_answer_as_the_direct_call(tools: LedgerTools) -> None:
    async def run() -> Any:
        async with Client(build_server(tools)) as client:
            return await client.call_tool("get_approval_matrix", {"company_id": "CO-A"})

    assert _payload(asyncio.run(run())) == call_tool(
        tools, "get_approval_matrix", {"company_id": "CO-A"}
    )


def test_invalid_input_is_an_error_result(tools: LedgerTools) -> None:
    async def run() -> Any:
        async with Client(build_server(tools)) as client:
            return await client.call_tool("get_counterparty_profile", {"counterparty_id": "V-0"})

    assert asyncio.run(run()).is_error is True


def test_propose_adjustment_over_mcp_only_drafts(tools: LedgerTools, store: LedgerStore) -> None:
    row = store.ledger_rows[0]
    arguments = {
        "target_row_id": row.row_id,
        "adjustment_type": "hold_payment",
        "amount_minor": row.amount_minor,
        "currency": row.currency,
        "reason": "Hold until the vendor is verified.",
        "evidence_row_ids": [row.row_id],
    }

    async def run() -> Any:
        async with Client(build_server(tools)) as client:
            return await client.call_tool("propose_adjustment", arguments)

    assert _payload(asyncio.run(run()))["status"] == "pending_human_approval"
    assert len(tools.queue.all_drafts()) == 1


def test_server_over_real_stdio_subprocess(dataset_root: Path, tmp_path: Path) -> None:
    """The documented command, as a separate process, through the SDK's stdio client."""
    data_root = dataset_root.parent
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "ledger_sleuth.cli",
            "--data-root",
            str(data_root),
            "--run-root",
            str(tmp_path / "runs"),
            "mcp",
            "--seed",
            str(TRAIN_SEED),
        ],
    )

    async def run() -> tuple[Any, Any]:
        async with Client(params) as client:
            listed = await client.list_tools()
            counts = await client.call_tool("run_matcher", {})
            return listed, counts

    listed, counts = asyncio.run(run())
    assert {t.name for t in listed.tools} == set(SPECS_BY_NAME)
    assert _payload(counts)["counts"]["matched"] > 20_000
