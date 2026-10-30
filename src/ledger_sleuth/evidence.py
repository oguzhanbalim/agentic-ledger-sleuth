"""The deterministic reviewer gate. Code, not model opinion.

A case file that fails here is rejected before any model reviews it. Every cited row must exist,
every asserted amount and date must be supported by the rows cited for it, every disposition must
rest on a claim, and any number in free text must trace to the citations. That last check is what
gives "the model never does arithmetic" teeth: a figure it worked out itself has nowhere to come
from. Numbers spelled out as words are not parsed; this is a gate against unsupported figures, not
a proof of correctness.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from ledger_sleuth.agent.schemas import AmountAssertion, CaseFile, Claim, DateAssertion
from ledger_sleuth.models import BankRow, LedgerRow, format_minor
from ledger_sleuth.tools import LedgerTools

_NUMBER = re.compile(r"\d[\d.,:]*\d|\d")
_IBAN = re.compile(r"\bXX\d{2}(?: ?\d)+\b")
_ISO_DATETIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?\b")
# L-000123, OI-L-000123, INV-1234-567890, INV-1234-567890/2, CO-A, F-1a2b3c
_IDENTIFIER = re.compile(r"\b[A-Za-z]{1,6}(?:-[A-Za-z0-9]+)+(?:/\d+)?")


@dataclass(slots=True)
class EvidenceReport:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _assertion_values(
    assertion: AmountAssertion, ledger_rows: list[LedgerRow], bank_rows: list[BankRow]
) -> tuple[list[int], list[str]]:
    """Values the assertion is compared with, and rows ruled out because of their currency."""
    if assertion.currency_basis == "try_equivalent":
        return [r.amount_try_minor for r in ledger_rows], []
    values, excluded = [], []
    for row in ledger_rows:
        if row.currency == assertion.currency:
            values.append(row.amount_minor)
        else:
            excluded.append(row.row_id)
    for bank in bank_rows:
        if bank.currency == assertion.currency:
            values.append(abs(bank.amount_minor))
        else:
            excluded.append(bank.bank_row_id)
    return values, excluded


def _check_amount(
    number: int,
    assertion: AmountAssertion,
    ledger_rows: list[LedgerRow],
    bank_rows: list[BankRow],
) -> list[str]:
    prefix = f"claim {number}: amount {assertion.amount_minor} ({assertion.basis})"
    if assertion.currency_basis == "try_equivalent" and assertion.currency != "TRY":
        return [f"{prefix}: try_equivalent basis requires currency TRY"]
    values, excluded = _assertion_values(assertion, ledger_rows, bank_rows)
    if not values:
        return [f"{prefix}: no cited row is in {assertion.currency}"]
    if excluded and assertion.basis != "any":
        return [f"{prefix}: cited rows in another currency: {', '.join(excluded)}"]
    supported = {
        "each": all(v == assertion.amount_minor for v in values),
        "any": assertion.amount_minor in values,
        "sum": sum(values) == assertion.amount_minor,
    }[assertion.basis]
    if supported:
        return []
    return [f"{prefix}: not supported by the cited rows, whose values are {sorted(values)}"]


def _check_date(
    number: int,
    assertion: DateAssertion,
    ledger_rows: list[LedgerRow],
    bank_rows: list[BankRow],
) -> list[str]:
    prefix = f"claim {number}: date {assertion.date.isoformat()} ({assertion.basis})"
    per_row = [{r.value_date, r.posting_ts.date()} for r in ledger_rows]
    per_row += [{r.value_date} for r in bank_rows]
    if not per_row:
        return [f"{prefix}: no known rows cited to support it"]
    hits = [assertion.date in dates for dates in per_row]
    if all(hits) if assertion.basis == "each" else any(hits):
        return []
    return [f"{prefix}: not a value or posting date of the cited rows"]


def _number_forms(amounts: Iterable[int]) -> set[str]:
    forms: set[str] = set()
    for amount in amounts:
        magnitude = abs(amount)
        whole, frac = divmod(magnitude, 100)
        formatted = format_minor(magnitude)
        forms |= {str(magnitude), formatted, f"{whole:,}", f"{whole:,}.{frac:02d}"}
        if frac == 0:
            forms.add(str(whole))
    return forms


def _traceable_numbers(tools: LedgerTools, claims: Iterable[Claim]) -> set[str]:
    """Every numeric token a statement resting on ``claims`` may contain."""
    allowed: set[str] = set()
    amounts: set[int] = set()
    for tier in tools.store.approval_matrix:
        if tier.max_amount_try_minor is not None:
            amounts.add(tier.max_amount_try_minor)
    for claim in claims:
        allowed.add(str(len(claim.row_ids)))
        if claim.amount is not None:
            amounts.add(claim.amount.amount_minor)
        for row_id in claim.row_ids:
            row = tools.store.ledger_by_id.get(row_id)
            bank = tools.store.bank_by_id.get(row_id)
            if row is not None:
                amounts |= {row.amount_minor, row.amount_try_minor}
                ts = row.posting_ts
                allowed |= {
                    str(row.value_date.day),
                    str(row.value_date.year),
                    str(ts.day),
                    str(ts.hour),
                    f"{ts.hour:02d}:{ts.minute:02d}",
                }
            if bank is not None:
                amounts.add(bank.amount_minor)
                allowed |= {str(bank.value_date.day), str(bank.value_date.year)}
        for finding_id in claim.finding_ids:
            finding = tools.finding(finding_id)
            if finding is not None:
                numeric = [v for v in finding.metrics.values() if isinstance(v, int)]
                allowed |= {str(v) for v in numeric}
                amounts.update(numeric)
    return allowed | _number_forms(amounts)


def _untraceable_numbers(text: str, allowed: set[str]) -> list[str]:
    stripped = _IDENTIFIER.sub(" ", _IBAN.sub(" ", _ISO_DATETIME.sub(" ", text)))
    return [t for t in _NUMBER.findall(stripped) if t not in allowed]


def _rests_on_a_claim(rows: Collection[str], finding_id: str | None, claims: list[Claim]) -> bool:
    return any(
        (finding_id is not None and finding_id in claim.finding_ids)
        or not set(rows).isdisjoint(claim.row_ids)
        for claim in claims
    )


def check_case_file(
    case: CaseFile,
    tools: LedgerTools,
    *,
    expected_finding_ids: Collection[str] = (),
    expected_item_ids: Collection[str] = (),
) -> EvidenceReport:
    report = EvidenceReport()
    errors = report.errors
    ledger = tools.store.ledger_by_id
    bank = tools.store.bank_by_id

    if not case.claims:
        errors.append("a case file must carry at least one claim")

    for number, claim in enumerate(case.claims, start=1):
        if not claim.row_ids:
            errors.append(f"claim {number}: cites no rows")
        unknown = [r for r in claim.row_ids if r not in ledger and r not in bank]
        if unknown:
            errors.append(f"claim {number}: unknown row IDs: {', '.join(unknown)}")
        unknown_findings = [f for f in claim.finding_ids if tools.finding(f) is None]
        if unknown_findings:
            errors.append(f"claim {number}: unknown finding IDs: {', '.join(unknown_findings)}")
        ledger_rows = [ledger[r] for r in claim.row_ids if r in ledger]
        bank_rows = [bank[r] for r in claim.row_ids if r in bank]
        if claim.amount is not None:
            errors.extend(_check_amount(number, claim.amount, ledger_rows, bank_rows))
        if claim.date is not None:
            errors.extend(_check_date(number, claim.date, ledger_rows, bank_rows))
        bad = _untraceable_numbers(claim.statement, _traceable_numbers(tools, [claim]))
        if bad:
            errors.append(
                f"claim {number}: statement has numbers not traceable to its citations: "
                f"{', '.join(bad)}. Copy figures from tool output and cite their source."
            )

    everything = _traceable_numbers(tools, case.claims)
    for label, text in (("summary", case.summary), ("recommended_action", case.recommended_action)):
        bad = _untraceable_numbers(text, everything)
        if bad:
            errors.append(f"{label}: numbers not traceable to any claim: {', '.join(bad)}")

    disposed_findings: set[str] = set()
    for group in case.finding_dispositions:
        for finding_id in group.finding_ids:
            disposed_findings.add(finding_id)
            finding = tools.finding(finding_id)
            if finding is None:
                errors.append(f"disposition for unknown finding {finding_id}")
            elif not _rests_on_a_claim(finding.row_ids, finding_id, case.claims):
                errors.append(f"finding {finding_id}: disposition rests on no claim")
    missing = sorted(set(expected_finding_ids) - disposed_findings)
    if missing:
        errors.append(f"findings left without a disposition: {', '.join(missing)}")
    outside = sorted(disposed_findings - set(expected_finding_ids)) if expected_finding_ids else []
    if outside:
        errors.append(f"dispositions for findings outside this cluster: {', '.join(outside)}")

    items = {item.item_id: item for item in tools.open_items}
    disposed_items: set[str] = set()
    for item_group in case.open_item_dispositions:
        for item_id in item_group.item_ids:
            disposed_items.add(item_id)
            open_item = items.get(item_id)
            if open_item is None:
                errors.append(f"disposition for unknown open item {item_id}")
                continue
            rows = [open_item.ledger_row_id or "", *open_item.bank_row_ids]
            if not _rests_on_a_claim(rows, None, case.claims):
                errors.append(f"open item {item_id}: disposition rests on no claim")
    missing_items = sorted(set(expected_item_ids) - disposed_items)
    if missing_items:
        errors.append(f"open items left without a disposition: {', '.join(missing_items)}")

    cited = {row for claim in case.claims for row in claim.row_ids}
    for draft_id in case.adjustment_draft_ids:
        draft = tools.queue.get(draft_id)
        if draft is None:
            errors.append(f"unknown adjustment draft {draft_id}")
        elif draft.target_row_id not in cited:
            errors.append(
                f"adjustment draft {draft_id}: target row {draft.target_row_id} is not cited "
                "by any claim"
            )
    return report
