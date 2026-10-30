from __future__ import annotations

from typing import Any

import pytest

from ledger_sleuth.agent.schemas import (
    AmountAssertion,
    CaseFile,
    Claim,
    DateAssertion,
    FindingDisposition,
    OpenItemDisposition,
)
from ledger_sleuth.evidence import check_case_file
from ledger_sleuth.models import LedgerRow, format_minor
from ledger_sleuth.tools import LedgerTools, call_tool


def _case(*claims: Claim, **fields: Any) -> CaseFile:
    defaults: dict[str, Any] = {
        "cluster_id": "CL-1",
        "disposition": "suspicious",
        "summary": "A summary.",
        "claims": list(claims),
        "finding_dispositions": [],
        "open_item_dispositions": [],
        "adjustment_draft_ids": [],
        "recommended_action": "Refer to a person.",
    }
    return CaseFile(**{**defaults, **fields})


def _claim(row_ids: list[str], statement: str = "Rows reviewed.", **fields: Any) -> Claim:
    defaults: dict[str, Any] = {"finding_ids": [], "amount": None, "date": None}
    return Claim(statement=statement, row_ids=row_ids, **{**defaults, **fields})


def _amount(
    value: int, currency: str, basis: str = "each", on: str = "transaction"
) -> AmountAssertion:
    return AmountAssertion(amount_minor=value, currency=currency, basis=basis, currency_basis=on)


@pytest.fixture
def row(tools: LedgerTools) -> LedgerRow:
    return next(
        r for r in tools.store.ledger_rows if r.currency == "EUR" and r.doc_type == "PAYMENT"
    )


def test_supported_case_passes(tools: LedgerTools, row: LedgerRow) -> None:
    claim = _claim(
        [row.row_id],
        f"Payment of {format_minor(row.amount_minor)} EUR.",
        amount=_amount(row.amount_minor, "EUR"),
        date=DateAssertion(date=row.value_date, basis="each"),
    )
    assert check_case_file(_case(claim), tools).ok


def test_unknown_row_is_rejected(tools: LedgerTools) -> None:
    report = check_case_file(_case(_claim(["L-999999"])), tools)
    assert not report.ok
    assert "unknown row IDs: L-999999" in report.errors[0]


def test_claim_without_rows_and_case_without_claims_are_rejected(tools: LedgerTools) -> None:
    assert "cites no rows" in check_case_file(_case(_claim([])), tools).errors[0]
    assert "at least one claim" in check_case_file(_case(), tools).errors[0]


def test_wrong_amount_is_rejected(tools: LedgerTools, row: LedgerRow) -> None:
    claim = _claim([row.row_id], amount=_amount(row.amount_minor + 1, "EUR"))
    report = check_case_file(_case(claim), tools)
    assert not report.ok
    assert "not supported by the cited rows" in report.errors[0]


def test_wrong_date_is_rejected(tools: LedgerTools, row: LedgerRow) -> None:
    from datetime import date

    claim = _claim([row.row_id], date=DateAssertion(date=date(2001, 1, 1), basis="any"))
    assert "not a value or posting date" in check_case_file(_case(claim), tools).errors[0]


def test_sum_is_recomputed_in_code(tools: LedgerTools) -> None:
    rows = [r for r in tools.store.ledger_rows if r.currency == "TRY"][:3]
    total = sum(r.amount_minor for r in rows)
    ids = [r.row_id for r in rows]
    assert check_case_file(_case(_claim(ids, amount=_amount(total, "TRY", "sum"))), tools).ok
    assert not check_case_file(
        _case(_claim(ids, amount=_amount(total + 1, "TRY", "sum"))), tools
    ).ok


def test_each_requires_every_row_and_any_requires_one(tools: LedgerTools) -> None:
    rows = [r for r in tools.store.ledger_rows if r.currency == "TRY"][:2]
    assert rows[0].amount_minor != rows[1].amount_minor
    ids = [r.row_id for r in rows]
    value = rows[0].amount_minor
    assert not check_case_file(_case(_claim(ids, amount=_amount(value, "TRY", "each"))), tools).ok
    assert check_case_file(_case(_claim(ids, amount=_amount(value, "TRY", "any"))), tools).ok


def test_currency_mismatch_is_rejected(tools: LedgerTools, row: LedgerRow) -> None:
    claim = _claim([row.row_id], amount=_amount(row.amount_minor, "USD"))
    assert "no cited row is in USD" in check_case_file(_case(claim), tools).errors[0]


def test_try_equivalent_basis(tools: LedgerTools, row: LedgerRow) -> None:
    good = _claim([row.row_id], amount=_amount(row.amount_try_minor, "TRY", on="try_equivalent"))
    assert check_case_file(_case(good), tools).ok
    bad = _claim([row.row_id], amount=_amount(row.amount_try_minor, "EUR", on="try_equivalent"))
    assert "requires currency TRY" in check_case_file(_case(bad), tools).errors[0]


def test_bank_rows_can_support_an_amount(tools: LedgerTools) -> None:
    bank = tools.store.bank_rows[0]
    claim = _claim(
        [bank.bank_row_id],
        amount=_amount(abs(bank.amount_minor), bank.currency),
        date=DateAssertion(date=bank.value_date, basis="each"),
    )
    assert check_case_file(_case(claim), tools).ok


@pytest.mark.parametrize("invented", ["412345.67", "12%", "99 days"])
def test_invented_number_in_a_statement_is_rejected(
    tools: LedgerTools, row: LedgerRow, invented: str
) -> None:
    report = check_case_file(_case(_claim([row.row_id], f"The total was {invented}.")), tools)
    assert not report.ok
    assert "not traceable" in report.errors[0]


def test_number_from_a_cited_finding_metric_is_allowed(tools: LedgerTools) -> None:
    finding = next(f for f in tools.findings if f.typology == "duplicate_payment")
    gap = finding.metrics["gap_days"]
    statement = f"Paid twice, {gap} days apart."
    allowed = _claim(list(finding.row_ids), statement, finding_ids=[finding.finding_id])
    assert check_case_file(_case(allowed), tools).ok


def test_identifiers_dates_and_ibans_do_not_count_as_numbers(
    tools: LedgerTools, row: LedgerRow
) -> None:
    statement = (
        f"Row {row.row_id} ({row.doc_no}), reference {row.reference}, on "
        f"{row.value_date.isoformat()}, paid to XX12 3456 7890."
    )
    assert check_case_file(_case(_claim([row.row_id], statement)), tools).ok


def test_thousands_separators_are_accepted(tools: LedgerTools, row: LedgerRow) -> None:
    whole = row.amount_minor // 100
    frac = row.amount_minor % 100
    statement = f"Paid {whole:,}.{frac:02d} EUR."
    assert check_case_file(_case(_claim([row.row_id], statement)), tools).ok


def test_summary_numbers_are_checked_too(tools: LedgerTools, row: LedgerRow) -> None:
    report = check_case_file(_case(_claim([row.row_id]), summary="Exposure of 777123.45."), tools)
    assert "summary: numbers not traceable" in report.errors[0]


def test_every_cluster_member_needs_a_disposition(tools: LedgerTools) -> None:
    finding = tools.findings[0]
    case = _case(_claim(list(finding.row_ids)))
    report = check_case_file(case, tools, expected_finding_ids=[finding.finding_id])
    assert "without a disposition" in report.errors[0]


def test_disposition_must_rest_on_a_claim(tools: LedgerTools) -> None:
    finding, other = tools.findings[0], tools.findings[1]
    assert set(finding.row_ids).isdisjoint(other.row_ids)
    case = _case(
        _claim(list(other.row_ids)),
        finding_dispositions=[
            FindingDisposition(
                finding_ids=[finding.finding_id], disposition="benign", reason="Looks fine."
            )
        ],
    )
    assert "rests on no claim" in check_case_file(case, tools).errors[0]


def test_closing_an_open_item_must_cite_it(tools: LedgerTools) -> None:
    item = tools.open_items[0]
    disposition = OpenItemDisposition(
        item_ids=[item.item_id], disposition="close_benign", reason="Timing."
    )
    unrelated = next(r.row_id for r in tools.store.ledger_rows if r.row_id != item.ledger_row_id)
    rejected = _case(_claim([unrelated]), open_item_dispositions=[disposition])
    assert "rests on no claim" in check_case_file(rejected, tools).errors[0]
    accepted = _case(_claim([item.ledger_row_id or ""]), open_item_dispositions=[disposition])
    assert check_case_file(accepted, tools, expected_item_ids=[item.item_id]).ok


def test_disposition_outside_the_cluster_is_rejected(tools: LedgerTools) -> None:
    finding, other = tools.findings[0], tools.findings[1]
    case = _case(
        _claim([*finding.row_ids, *other.row_ids]),
        finding_dispositions=[
            FindingDisposition(
                finding_ids=[finding.finding_id, other.finding_id],
                disposition="benign",
                reason="Both fine.",
            )
        ],
    )
    report = check_case_file(case, tools, expected_finding_ids=[finding.finding_id])
    assert any("outside this cluster" in e for e in report.errors)


def test_adjustment_draft_must_exist_and_its_target_be_cited(
    tools: LedgerTools, row: LedgerRow
) -> None:
    missing = _case(_claim([row.row_id]), adjustment_draft_ids=["ADJ-nope"])
    assert "unknown adjustment draft" in check_case_file(missing, tools).errors[0]

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
    other = next(r.row_id for r in tools.store.ledger_rows if r.row_id != row.row_id)
    uncited = _case(_claim([other]), adjustment_draft_ids=[draft["draft_id"]])
    assert "is not cited" in check_case_file(uncited, tools).errors[0]
    cited = _case(_claim([row.row_id]), adjustment_draft_ids=[draft["draft_id"]])
    assert check_case_file(cited, tools).ok
