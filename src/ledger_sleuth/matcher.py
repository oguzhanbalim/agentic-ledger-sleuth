"""Deterministic ledger-to-bank matcher.

Passes run from strictest to loosest, so a bank row is consumed by its best explanation. Whatever
is left becomes an open item with the numbers needed to reason about it, all computed here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ledger_sleuth.models import CASH_DOC_TYPES, BankRow, LedgerRow
from ledger_sleuth.store import LedgerStore

DATE_TOLERANCE_DAYS = 3
AMOUNT_ONLY_WINDOW_DAYS = 5
FX_TOLERANCE_BPS = 300
FUZZY_MIN_SIMILARITY_PCT = 45

OPEN_KINDS = (
    "timing_difference",
    "fx_difference",
    "partial_payment",
    "amount_mismatch",
    "reference_mismatch",
    "unmatched_ledger",
    "unmatched_bank",
)


@dataclass(frozen=True, slots=True)
class OpenItem:
    item_id: str
    kind: str
    company_id: str
    ledger_row_id: str | None
    bank_row_ids: tuple[str, ...]
    counterparty_id: str | None
    metrics: dict[str, int | str]


@dataclass(slots=True)
class MatchResult:
    matched: dict[str, tuple[str, ...]] = field(default_factory=dict)
    open_items: list[OpenItem] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        counts["matched"] = len(self.matched)
        for item in self.open_items:
            counts[item.kind] += 1
        return dict(counts)


def normalize_reference(reference: str) -> str:
    return "".join(ch for ch in reference.upper() if ch.isalnum())


def similarity_pct(left: str, right: str) -> int:
    """Similarity of two strings after normalisation, as an integer percentage.

    One string embedded in the other, as a reference in remittance text, scores at least 90.
    """
    a, b = normalize_reference(left), normalize_reference(right)
    if not a or not b:
        return 0
    if a in b or b in a:
        shorter, longer = sorted((len(a), len(b)))
        return max(90, round(100 * shorter / longer))
    return round(100 * SequenceMatcher(None, a, b, autojunk=False).ratio())


def _signed(row: LedgerRow) -> int:
    return -row.amount_minor if row.direction == "out" else row.amount_minor


def run_matcher(store: LedgerStore) -> MatchResult:
    ledger = [r for r in store.ledger_rows if r.doc_type in CASH_DOC_TYPES]
    bank_free: dict[str, BankRow] = dict(store.bank_by_id)

    by_ref: dict[tuple[str, str], list[BankRow]] = defaultdict(list)
    by_amount: dict[tuple[str, str, int], list[BankRow]] = defaultdict(list)
    for bank in store.bank_rows:
        by_ref[(bank.company_id, normalize_reference(bank.reference))].append(bank)
        by_amount[(bank.company_id, bank.currency, bank.amount_minor)].append(bank)

    result = MatchResult()
    remaining: list[LedgerRow] = []

    def available(rows: list[BankRow]) -> list[BankRow]:
        return [b for b in rows if b.bank_row_id in bank_free]

    def consume(rows: list[BankRow]) -> None:
        for bank in rows:
            bank_free.pop(bank.bank_row_id, None)

    def add_open(row: LedgerRow, kind: str, banks: list[BankRow], **metrics: int | str) -> None:
        consume(banks)
        result.open_items.append(
            OpenItem(
                item_id=f"OI-{row.row_id}",
                kind=kind,
                company_id=row.company_id,
                ledger_row_id=row.row_id,
                bank_row_ids=tuple(b.bank_row_id for b in banks),
                counterparty_id=row.counterparty_id,
                metrics=metrics,
            )
        )

    def ref_candidates(row: LedgerRow) -> list[BankRow]:
        key = (row.company_id, normalize_reference(row.reference))
        return sorted(
            available(by_ref.get(key, [])),
            key=lambda b: (abs((b.value_date - row.value_date).days), b.bank_row_id),
        )

    # Pass 1: same reference, same amount and currency, dates close together.
    for row in ledger:
        exact = [
            b
            for b in ref_candidates(row)
            if b.currency == row.currency
            and b.amount_minor == _signed(row)
            and abs((b.value_date - row.value_date).days) <= DATE_TOLERANCE_DAYS
        ]
        if exact:
            consume(exact[:1])
            result.matched[row.row_id] = (exact[0].bank_row_id,)
        else:
            remaining.append(row)

    # Pass 2: same reference, something else differs.
    unresolved: list[LedgerRow] = []
    for row in remaining:
        candidates = ref_candidates(row)
        same_amount = [
            b for b in candidates if b.currency == row.currency and b.amount_minor == _signed(row)
        ]
        if same_amount:
            bank = same_amount[0]
            gap = abs((bank.value_date - row.value_date).days)
            add_open(row, "timing_difference", [bank], gap_days=gap)
            continue
        same_currency = [b for b in candidates if b.currency == row.currency]
        if same_currency:
            total = sum(abs(b.amount_minor) for b in same_currency)
            shortfall = row.amount_minor - total
            kind = "partial_payment" if shortfall >= 0 else "amount_mismatch"
            add_open(
                row,
                kind,
                same_currency,
                ledger_amount_minor=row.amount_minor,
                bank_total_minor=total,
                shortfall_minor=shortfall,
                tranches=len(same_currency),
            )
            continue
        foreign = [b for b in candidates if b.currency == "TRY" and row.currency != "TRY"]
        if foreign:
            bank = foreign[0]
            diff = abs(bank.amount_minor) - row.amount_try_minor
            bps = round(10_000 * diff / max(row.amount_try_minor, 1))
            if abs(bps) <= FX_TOLERANCE_BPS:
                add_open(
                    row,
                    "fx_difference",
                    [bank],
                    ledger_try_minor=row.amount_try_minor,
                    bank_try_minor=abs(bank.amount_minor),
                    diff_minor=diff,
                    diff_bps=bps,
                )
            else:
                add_open(
                    row,
                    "amount_mismatch",
                    [bank],
                    ledger_try_minor=row.amount_try_minor,
                    bank_try_minor=abs(bank.amount_minor),
                    diff_minor=diff,
                    diff_bps=bps,
                )
            continue
        unresolved.append(row)

    # Pass 3: reference differs, amount and currency agree, dates close, references comparable.
    for row in unresolved:
        key = (row.company_id, row.currency, _signed(row))
        candidates = [
            b
            for b in available(by_amount.get(key, []))
            if abs((b.value_date - row.value_date).days) <= AMOUNT_ONLY_WINDOW_DAYS
        ]
        scored = sorted(
            ((similarity_pct(row.reference, b.reference), b) for b in candidates),
            key=lambda pair: (-pair[0], pair[1].bank_row_id),
        )
        if scored and scored[0][0] >= FUZZY_MIN_SIMILARITY_PCT:
            similarity, bank = scored[0]
            add_open(
                row,
                "reference_mismatch",
                [bank],
                similarity_pct=similarity,
                ledger_reference=row.reference,
                bank_reference=bank.reference,
                gap_days=abs((bank.value_date - row.value_date).days),
            )
        else:
            add_open(row, "unmatched_ledger", [])

    for bank in sorted(bank_free.values(), key=lambda b: b.bank_row_id):
        result.open_items.append(
            OpenItem(
                item_id=f"OI-{bank.bank_row_id}",
                kind="unmatched_bank",
                company_id=bank.company_id,
                ledger_row_id=None,
                bank_row_ids=(bank.bank_row_id,),
                counterparty_id=None,
                metrics={"amount_minor": bank.amount_minor},
            )
        )
    result.open_items.sort(key=lambda item: item.item_id)
    return result
