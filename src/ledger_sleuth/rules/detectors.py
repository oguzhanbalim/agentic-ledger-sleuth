"""One deterministic detector per typology. Every number on a finding is computed here."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from ledger_sleuth.matcher import normalize_reference
from ledger_sleuth.models import SYSTEM_USER, LedgerRow
from ledger_sleuth.store import LedgerStore

# duplicate_payment
DUPLICATE_WINDOW_DAYS = 14
# invoice_split
SPLIT_WINDOW_DAYS = 7
SPLIT_MIN_PART_RATIO_PCT = 25
# bank_detail_change
BANK_CHANGE_WINDOW_DAYS = 14
# just_below_threshold
BELOW_LIMIT_BAND_BPS = 200
# off_hours_posting
WORK_START_HOUR = 7
WORK_END_HOUR = 20
# new_vendor_high_value
NEW_VENDOR_DAYS = 30
NEW_VENDOR_HIGH_VALUE_TRY_MINOR = 250_000_00
# benford_deviation
BENFORD_MIN_INVOICES = 30
BENFORD_CHI2_CRITICAL = 20.09  # chi-square, 8 degrees of freedom, p = 0.01
BENFORD_MIN_MAD_BPS = 400  # mean absolute deviation; separates effect size from sample size

BENFORD_EXPECTED = tuple(math.log10(1 + 1 / digit) for digit in range(1, 10))


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    typology: str
    company_id: str
    counterparty_id: str | None
    row_ids: tuple[str, ...]
    summary: str
    metrics: dict[str, int | str]


def _finding(
    typology: str,
    company_id: str,
    counterparty_id: str | None,
    row_ids: list[str],
    summary: str,
    **metrics: int | str,
) -> Finding:
    unique = tuple(sorted(set(row_ids)))
    key = f"{typology}|{counterparty_id}|{','.join(unique)}"
    finding_id = f"F-{hashlib.sha256(key.encode()).hexdigest()[:10]}"
    return Finding(finding_id, typology, company_id, counterparty_id, unique, summary, metrics)


def detect_duplicate_payments(store: LedgerStore) -> list[Finding]:
    """Same vendor, same currency and exact amount, paid twice within the window."""
    groups: dict[tuple[str, str, int], list[LedgerRow]] = defaultdict(list)
    for row in store.ledger_rows:
        if row.doc_type == "PAYMENT" and row.counterparty_id:
            groups[(row.counterparty_id, row.currency, row.amount_minor)].append(row)
    findings = []
    for (cp, _, _), rows in groups.items():
        rows.sort(key=lambda r: (r.value_date, r.row_id))
        for earlier, later in pairwise(rows):
            gap = (later.value_date - earlier.value_date).days
            same_ref = normalize_reference(earlier.reference) == normalize_reference(
                later.reference
            )
            if gap <= DUPLICATE_WINDOW_DAYS or same_ref:
                findings.append(
                    _finding(
                        "duplicate_payment",
                        later.company_id,
                        cp,
                        [earlier.row_id, later.row_id],
                        f"Two payments of the same amount to {cp}, {gap} days apart.",
                        gap_days=gap,
                        amount_minor=later.amount_minor,
                        currency=later.currency,
                        same_reference=str(same_ref).lower(),
                    )
                )
    return findings


def detect_invoice_splits(store: LedgerStore) -> list[Finding]:
    """Invoices that sit below the lowest limit alone and exceed it together, within the window."""
    groups: dict[tuple[str, str], list[LedgerRow]] = defaultdict(list)
    for row in store.ledger_rows:
        if row.doc_type == "INVOICE" and row.counterparty_id:
            groups[(row.company_id, row.counterparty_id)].append(row)
    findings = []
    for (company, cp), rows in groups.items():
        limit = store.limits(company)[0]
        floor = limit * SPLIT_MIN_PART_RATIO_PCT // 100
        parts = sorted(
            (r for r in rows if floor <= r.amount_try_minor < limit),
            key=lambda r: (r.value_date, r.row_id),
        )
        index = 0
        while index < len(parts):
            burst = [parts[index]]
            while (
                index + len(burst) < len(parts)
                and (parts[index + len(burst)].value_date - burst[0].value_date).days
                <= SPLIT_WINDOW_DAYS
            ):
                burst.append(parts[index + len(burst)])
            total = sum(r.amount_try_minor for r in burst)
            if len(burst) >= 2 and total > limit:
                findings.append(
                    _finding(
                        "invoice_split",
                        company,
                        cp,
                        [r.row_id for r in burst],
                        f"{len(burst)} invoices from {cp}, each under the limit, together over it.",
                        invoices=len(burst),
                        total_try_minor=total,
                        limit_try_minor=limit,
                        span_days=(burst[-1].value_date - burst[0].value_date).days,
                    )
                )
            index += len(burst)
    return findings


def detect_bank_detail_changes(store: LedgerStore) -> list[Finding]:
    """A payment shortly after the vendor's IBAN changed. Verification is reported, not assumed."""
    findings = []
    for change in store.iban_changes:
        window_end = change.changed_on + timedelta(days=BANK_CHANGE_WINDOW_DAYS)
        payments = [
            r
            for r in store.rows_by_counterparty.get(change.counterparty_id, [])
            if r.doc_type == "PAYMENT" and change.changed_on <= r.value_date <= window_end
        ]
        if payments:
            first = min(payments, key=lambda r: r.value_date)
            findings.append(
                _finding(
                    "bank_detail_change",
                    first.company_id,
                    change.counterparty_id,
                    [r.row_id for r in payments],
                    f"{len(payments)} payment(s) to {change.counterparty_id} within the window "
                    "after an IBAN change.",
                    change_id=change.change_id,
                    changed_on=change.changed_on.isoformat(),
                    changed_by=change.changed_by,
                    verified_by=change.verified_by or "none",
                    days_to_first_payment=(first.value_date - change.changed_on).days,
                    payments=len(payments),
                )
            )
    return findings


def detect_just_below_threshold(store: LedgerStore) -> list[Finding]:
    """An invoice inside a narrow band under an approval limit."""
    limits = {company: store.limits(company) for company in store.company_ids}
    findings = []
    for row in store.ledger_rows:
        if row.doc_type != "INVOICE":
            continue
        for limit in limits[row.company_id]:
            gap_bps = (limit - row.amount_try_minor) * 10_000 // limit
            if 0 < row.amount_try_minor < limit and gap_bps <= BELOW_LIMIT_BAND_BPS:
                findings.append(
                    _finding(
                        "just_below_threshold",
                        row.company_id,
                        row.counterparty_id,
                        [row.row_id],
                        f"Invoice within {gap_bps} bps under an approval limit.",
                        amount_try_minor=row.amount_try_minor,
                        limit_try_minor=limit,
                        gap_bps=gap_bps,
                    )
                )
                break
    return findings


def _in_close_window(day: date) -> bool:
    return day.day <= 2 or (day + timedelta(days=2)).month != day.month


def detect_off_hours(store: LedgerStore) -> list[Finding]:
    """A manual posting outside working hours or at a weekend. Batch jobs exempt; closes are not."""
    totals: dict[str, int] = defaultdict(int)
    off: dict[str, list[LedgerRow]] = defaultdict(list)
    for row in store.ledger_rows:
        if row.posted_by == SYSTEM_USER:
            continue
        totals[row.posted_by] += 1
        ts = row.posting_ts
        if ts.weekday() >= 5 or not WORK_START_HOUR <= ts.hour < WORK_END_HOUR:
            off[row.posted_by].append(row)
    findings = []
    for user, rows in off.items():
        share_bps = len(rows) * 10_000 // totals[user]
        for row in rows:
            ts = row.posting_ts
            findings.append(
                _finding(
                    "off_hours_posting",
                    row.company_id,
                    row.counterparty_id,
                    [row.row_id],
                    f"{row.doc_type} posted by {user} at {ts.isoformat()}.",
                    posted_by=user,
                    hour=ts.hour,
                    weekday=ts.weekday(),
                    in_close_window=str(_in_close_window(ts.date())).lower(),
                    user_off_hours_share_bps=share_bps,
                    user_total_postings=totals[user],
                )
            )
    return findings


def detect_new_vendor_high_value(store: LedgerStore) -> list[Finding]:
    """A large payment to a vendor created only days earlier."""
    findings = []
    for cp_id, rows in store.rows_by_counterparty.items():
        cp = store.counterparties[cp_id]
        for row in rows:
            if row.doc_type != "PAYMENT" or row.amount_try_minor < NEW_VENDOR_HIGH_VALUE_TRY_MINOR:
                continue
            age = (row.value_date - cp.created_on).days
            if 0 <= age <= NEW_VENDOR_DAYS:
                findings.append(
                    _finding(
                        "new_vendor_high_value",
                        row.company_id,
                        cp_id,
                        [row.row_id],
                        f"Payment to {cp_id}, {age} days after the vendor was created.",
                        vendor_age_days=age,
                        created_on=cp.created_on.isoformat(),
                        amount_try_minor=row.amount_try_minor,
                    )
                )
    return findings


def leading_digit(amount_minor: int) -> int:
    """First significant digit of a positive amount."""
    return int(str(abs(amount_minor))[0])


def benford_statistics(amounts: list[int]) -> tuple[float, float]:
    """(chi-square, mean absolute deviation) of leading digits against Benford's law."""
    counts = [0] * 9
    for amount in amounts:
        counts[leading_digit(amount) - 1] += 1
    n = len(amounts)
    chi2 = sum(
        (observed - n * expected) ** 2 / (n * expected)
        for observed, expected in zip(counts, BENFORD_EXPECTED, strict=True)
    )
    mad = (
        sum(
            abs(observed / n - expected)
            for observed, expected in zip(counts, BENFORD_EXPECTED, strict=True)
        )
        / 9
    )
    return chi2, mad


def detect_benford_deviation(store: LedgerStore) -> list[Finding]:
    """Per-vendor leading-digit test on invoice amounts; needs both significance and effect size."""
    findings = []
    for cp_id, rows in store.rows_by_counterparty.items():
        invoices = [r for r in rows if r.doc_type == "INVOICE"]
        if len(invoices) < BENFORD_MIN_INVOICES:
            continue
        chi2, mad = benford_statistics([r.amount_minor for r in invoices])
        mad_bps = round(mad * 10_000)
        if chi2 > BENFORD_CHI2_CRITICAL and mad_bps >= BENFORD_MIN_MAD_BPS:
            findings.append(
                _finding(
                    "benford_deviation",
                    invoices[0].company_id,
                    cp_id,
                    [r.row_id for r in invoices],
                    f"Leading digits of {len(invoices)} invoices from {cp_id} are off Benford.",
                    invoices=len(invoices),
                    chi2_x100=round(chi2 * 100),
                    mad_bps=mad_bps,
                    distinct_amounts=len({r.amount_minor for r in invoices}),
                )
            )
    return findings


def normalize_iban(iban: str) -> str:
    return re.sub(r"\s+", "", iban).upper()


def normalize_address(address: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", address.upper()).split())


def detect_employee_vendor_overlap(store: LedgerStore) -> list[Finding]:
    """A vendor's current IBAN or address equals an employee's, after normalisation."""
    by_iban = {normalize_iban(e.iban): e for e in store.employees}
    by_address = {normalize_address(e.address): e for e in store.employees}
    findings = []
    for cp in store.counterparties.values():
        if cp.kind != "vendor":
            continue
        matches = []
        employee = by_iban.get(normalize_iban(cp.iban))
        if employee:
            matches.append(("iban", employee))
        employee = by_address.get(normalize_address(cp.address))
        if employee:
            matches.append(("address", employee))
        for field_name, match in matches[:1]:
            rows = store.rows_by_counterparty.get(cp.counterparty_id, [])
            findings.append(
                _finding(
                    "employee_vendor_overlap",
                    cp.company_ids[0],
                    cp.counterparty_id,
                    [r.row_id for r in rows if r.doc_type == "INVOICE"],
                    f"Vendor {cp.counterparty_id} shares a {field_name} with {match.employee_id}.",
                    matched_field=field_name,
                    employee_id=match.employee_id,
                    vendor_type=cp.vendor_type,
                )
            )
    return findings


ALL_DETECTORS: dict[str, Callable[[LedgerStore], list[Finding]]] = {
    "duplicate_payment": detect_duplicate_payments,
    "invoice_split": detect_invoice_splits,
    "bank_detail_change": detect_bank_detail_changes,
    "just_below_threshold": detect_just_below_threshold,
    "off_hours_posting": detect_off_hours,
    "new_vendor_high_value": detect_new_vendor_high_value,
    "benford_deviation": detect_benford_deviation,
    "employee_vendor_overlap": detect_employee_vendor_overlap,
}


def run_all_rules(store: LedgerStore) -> list[Finding]:
    findings = [f for detect in ALL_DETECTORS.values() for f in detect(store)]
    return sorted(findings, key=lambda f: f.finding_id)
