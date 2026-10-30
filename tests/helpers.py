"""Shared test doubles. Nothing here reaches the network."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

from anthropic.types import Message

from ledger_sleuth.generator.writer import SCHEMA
from ledger_sleuth.store import LEDGER_FILENAME, LedgerStore


def message(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    **extra: Any,
) -> Message:
    """A real SDK Message object, so the loop is exercised against the true response type."""
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
            **extra,
        }
    )


def text(payload: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return {"type": "text", "text": body}


def tool_use(name: str, tool_input: dict[str, Any], block_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


class ScriptedMessages:
    def __init__(self, script: list[Message | Exception]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("scripted client ran out of responses")
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ScriptedClient:
    """Plays back a fixed list of responses and records every request."""

    def __init__(self, script: list[Message | Exception]) -> None:
        self.messages = ScriptedMessages(script)


def build_store(tmp_path: Path, **tables: list[tuple[Any, ...]]) -> LedgerStore:
    """A tiny hand-written ledger, for pinning a rule's exact boundaries."""
    db = tmp_path / LEDGER_FILENAME
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO companies VALUES ('CO-A', 'Synthetic Company A', 'TRY')")
    conn.executemany(
        "INSERT INTO approval_matrix VALUES (?,?,?,?)",
        [
            ("CO-A", 1, 100_000_00, "AP_SUPERVISOR"),
            ("CO-A", 2, 500_000_00, "FINANCE_MANAGER"),
            ("CO-A", 3, None, "CFO"),
        ],
    )
    for table, rows in tables.items():
        if rows:
            marks = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    conn.commit()
    conn.close()
    return LedgerStore(tmp_path)


def ledger_row(
    row_id: str,
    *,
    doc_type: str = "INVOICE",
    counterparty: str | None = "V-1",
    ts: str = "2025-03-10T10:00:00",
    amount: int = 100_00,
    reference: str | None = None,
    posted_by: str = "E-1",
    currency: str = "TRY",
    amount_try: int | None = None,
    direction: str | None = None,
    company: str = "CO-A",
) -> tuple[Any, ...]:
    return (
        row_id,
        company,
        f"DOC-{row_id}",
        doc_type,
        direction or ("out" if doc_type == "PAYMENT" else "none"),
        counterparty,
        ts,
        ts[:10],
        currency,
        amount,
        1_000_000,
        amount if amount_try is None else amount_try,
        reference or f"REF-{row_id}",
        "Services rendered",
        "320-AP",
        posted_by,
        "E-2",
    )


def counterparty(
    cp_id: str = "V-1",
    *,
    created: str = "2020-01-01",
    iban: str = "XX000001",
    address: str = "Ardic Sokak No:1/1, Merkez, Izmir",
    vendor_type: str = "trade",
) -> tuple[Any, ...]:
    return (cp_id, f"Vendor {cp_id}", "vendor", vendor_type, "TRY", created, iban, address, "CO-A")
