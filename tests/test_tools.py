from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ledger_sleuth.store import LedgerStore
from ledger_sleuth.tools import (
    SPECS_BY_NAME,
    TOOL_SPECS,
    LedgerTools,
    PendingQueue,
    ToolError,
    call_tool,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hostile_calls(tools: LedgerTools) -> list[tuple[str, dict[str, Any]]]:
    finding = tools.findings[0]
    row = tools.store.ledger_rows[0]
    injection = "'; DROP TABLE ledger_rows; --"
    return [
        ("list_open_items", {}),
        ("list_open_items", {"kind": injection, "company_id": injection}),
        ("get_ledger_rows", {"row_ids": [row.row_id]}),
        ("get_ledger_rows", {"reference_contains": injection}),
        ("get_ledger_rows", {"row_ids": [injection]}),
        ("get_bank_rows", {"reference_contains": injection}),
        ("get_bank_rows", {"counterparty_iban": injection}),
        ("run_matcher", {}),
        ("run_matcher", {"ledger_row_id": row.row_id}),
        ("get_rule_findings", {"finding_ids": [finding.finding_id]}),
        ("get_rule_findings", {"typology": injection}),
        ("get_counterparty_profile", {"counterparty_id": row.counterparty_id or "V-0"}),
        ("get_approval_matrix", {"company_id": injection}),
        (
            "propose_adjustment",
            {
                "target_row_id": row.row_id,
                "adjustment_type": "hold_payment",
                "amount_minor": row.amount_minor,
                "currency": row.currency,
                "reason": injection + " padding",
                "evidence_row_ids": [row.row_id],
            },
        ),
    ]


def test_no_tool_call_changes_the_ledger(tools: LedgerTools, store: LedgerStore) -> None:
    before = _digest(store.path)
    rows_before = store.conn.execute(
        "SELECT COUNT(*), SUM(amount_minor) FROM ledger_rows"
    ).fetchone()
    exercised = set()
    for name, tool_input in _hostile_calls(tools):
        exercised.add(name)
        call_tool(tools, name, tool_input)
    assert exercised == set(SPECS_BY_NAME), "every tool must be exercised"
    assert _digest(store.path) == before
    assert (
        store.conn.execute("SELECT COUNT(*), SUM(amount_minor) FROM ledger_rows").fetchone()
        == rows_before
    )


def test_the_only_write_tool_is_propose_adjustment() -> None:
    writers = [spec.name for spec in TOOL_SPECS if not spec.read_only]
    assert writers == ["propose_adjustment"]


def test_no_way_to_post_or_approve_exists() -> None:
    banned = ("post", "approve", "commit", "execute", "delete", "update", "insert")
    for name in SPECS_BY_NAME:
        assert not any(word in name for word in banned), name
    for owner in (LedgerTools, PendingQueue):
        public = [n for n in dir(owner) if not n.startswith("_")]
        assert not any(word in n for n in public for word in banned), public


def test_draft_goes_to_the_queue_file_and_is_pending(
    tools: LedgerTools, store: LedgerStore, tmp_path: Path
) -> None:
    row = next(r for r in store.ledger_rows if r.doc_type == "PAYMENT")
    before = _digest(store.path)
    draft = call_tool(
        tools,
        "propose_adjustment",
        {
            "target_row_id": row.row_id,
            "adjustment_type": "reverse_duplicate_payment",
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "reason": "Second payment of the same invoice.",
            "evidence_row_ids": [row.row_id],
        },
    )
    assert draft["status"] == "pending_human_approval"
    assert draft["required_approver_role"] in {"AP_SUPERVISOR", "FINANCE_MANAGER", "CFO", "BOARD"}
    assert _digest(store.path) == before
    assert tools.queue.path.exists()
    assert tools.queue.path.parent == tmp_path
    assert len(tools.queue.all_drafts()) == 1


def test_repeating_a_proposal_does_not_duplicate_it(tools: LedgerTools, store: LedgerStore) -> None:
    row = store.ledger_rows[0]
    args = {
        "target_row_id": row.row_id,
        "adjustment_type": "hold_payment",
        "amount_minor": row.amount_minor,
        "currency": row.currency,
        "reason": "Hold until verified.",
        "evidence_row_ids": [row.row_id],
    }
    first = call_tool(tools, "propose_adjustment", args)
    second = call_tool(tools, "propose_adjustment", args)
    assert first["draft_id"] == second["draft_id"]
    assert len(tools.queue.all_drafts()) == 1


def test_withdrawn_draft_changes_status_only(tools: LedgerTools, store: LedgerStore) -> None:
    row = store.ledger_rows[0]
    draft = call_tool(
        tools,
        "propose_adjustment",
        {
            "target_row_id": row.row_id,
            "adjustment_type": "hold_payment",
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "reason": "Hold until verified.",
            "evidence_row_ids": [row.row_id],
        },
    )
    tools.queue.withdraw(draft["draft_id"], "case rejected")
    assert tools.queue.get(draft["draft_id"]).status == "withdrawn"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"target_row_id": "L-999999"}, "unknown target"),
        ({"evidence_row_ids": ["L-999999"]}, "unknown evidence"),
        ({"currency": "XXX"}, "invalid input"),
        ({"amount_minor": 0}, "invalid input"),
        ({"amount_minor": 10**15}, "exceeds target"),
        ({"reason": "short"}, "invalid input"),
        ({"evidence_row_ids": []}, "invalid input"),
        ({"adjustment_type": "post_to_ledger"}, "invalid input"),
        ({"status": "approved"}, "invalid input"),
    ],
)
def test_bad_proposals_are_rejected(
    tools: LedgerTools, store: LedgerStore, change: dict[str, Any], message: str
) -> None:
    row = store.ledger_rows[0]
    args: dict[str, Any] = {
        "target_row_id": row.row_id,
        "adjustment_type": "hold_payment",
        "amount_minor": row.amount_minor,
        "currency": row.currency,
        "reason": "A sufficiently long reason.",
        "evidence_row_ids": [row.row_id],
    }
    args.update(change)
    with pytest.raises(ToolError, match=message):
        call_tool(tools, "propose_adjustment", args)
    assert tools.queue.all_drafts() == []


def test_wrong_currency_proposal_is_rejected(tools: LedgerTools, store: LedgerStore) -> None:
    row = next(r for r in store.ledger_rows if r.currency == "TRY")
    with pytest.raises(ToolError, match="currency must match"):
        call_tool(
            tools,
            "propose_adjustment",
            {
                "target_row_id": row.row_id,
                "adjustment_type": "hold_payment",
                "amount_minor": 1,
                "currency": "EUR",
                "reason": "A sufficiently long reason.",
                "evidence_row_ids": [row.row_id],
            },
        )


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("get_ledger_rows", {"limit": 0}),
        ("get_ledger_rows", {"limit": 10_000}),
        ("get_ledger_rows", {"date_from": "not-a-date"}),
        ("get_ledger_rows", {"unexpected": 1}),
        ("get_rule_findings", {"offset": -1}),
        ("get_counterparty_profile", {}),
        ("no_such_tool", {}),
    ],
)
def test_invalid_inputs_become_tool_errors(
    tools: LedgerTools, name: str, tool_input: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        call_tool(tools, name, tool_input)


def test_totals_cover_all_filtered_rows_not_the_page(
    tools: LedgerTools, store: LedgerStore
) -> None:
    cp = next(c for c, rows in store.rows_by_counterparty.items() if len(rows) > 10)
    page = call_tool(tools, "get_ledger_rows", {"counterparty_id": cp, "limit": 2})
    rows = store.rows_by_counterparty[cp]
    assert page["returned"] == 2
    assert page["truncated"] is True
    assert page["total"] == len(rows)
    assert page["totals"]["amount_try_minor"] == sum(r.amount_try_minor for r in rows)


def test_tools_expose_no_ground_truth(tools: LedgerTools, truth: dict[str, Any]) -> None:
    inc = truth["incidents"][0]
    output = json.dumps(
        [
            call_tool(tools, "get_ledger_rows", {"row_ids": inc["row_ids"]}),
            call_tool(
                tools, "get_counterparty_profile", {"counterparty_id": inc["counterparty_id"]}
            ),
        ]
    )
    assert inc["incident_id"] not in output
    assert inc["variant"] not in output


def test_profile_sees_address_overlap_the_rule_misses(
    tools: LedgerTools, truth: dict[str, Any]
) -> None:
    """The abbreviated-address variant is a rule blind spot the investigator can still see."""
    incident = next(i for i in truth["incidents"] if i["variant"] == "address_abbreviated")
    profile = call_tool(
        tools, "get_counterparty_profile", {"counterparty_id": incident["counterparty_id"]}
    )
    assert any(link["address_similarity_pct"] >= 70 for link in profile["employee_links"])
