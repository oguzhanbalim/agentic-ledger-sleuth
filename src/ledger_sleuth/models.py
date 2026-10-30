"""Plain records shared by the generator, the rules and the tools.

All money is an integer in minor units (kurus/cents). Floats never hold an amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Currency = Literal["TRY", "EUR", "USD"]
DocType = Literal["INVOICE", "PAYMENT", "SALES_INVOICE", "RECEIPT", "REFUND", "JOURNAL"]
Direction = Literal["out", "in", "none"]

SYSTEM_USER = "SYS-BATCH"
CASH_DOC_TYPES: frozenset[str] = frozenset({"PAYMENT", "RECEIPT", "REFUND"})


@dataclass(frozen=True, slots=True)
class LedgerRow:
    row_id: str
    company_id: str
    doc_no: str
    doc_type: str
    direction: str
    counterparty_id: str | None
    posting_ts: datetime
    value_date: date
    currency: str
    amount_minor: int
    fx_rate_micro: int
    amount_try_minor: int
    reference: str
    description: str
    account: str
    posted_by: str
    approved_by: str | None


@dataclass(frozen=True, slots=True)
class BankRow:
    bank_row_id: str
    company_id: str
    account_id: str
    value_date: date
    currency: str
    amount_minor: int  # signed: negative leaves the account
    counterparty_iban: str
    channel: str
    reference: str


@dataclass(frozen=True, slots=True)
class Counterparty:
    counterparty_id: str
    name: str
    kind: str  # vendor | customer
    vendor_type: str  # trade | employee_expense | customer
    currency: str
    created_on: date
    iban: str
    address: str
    company_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IbanChange:
    change_id: str
    counterparty_id: str
    changed_on: date
    old_iban: str
    new_iban: str
    changed_by: str
    verified_by: str | None


@dataclass(frozen=True, slots=True)
class Employee:
    employee_id: str
    name: str
    role: str
    company_id: str
    iban: str
    address: str


@dataclass(frozen=True, slots=True)
class ApprovalTier:
    company_id: str
    tier: int
    max_amount_try_minor: int | None  # None: no upper limit
    approver_role: str


def format_minor(amount_minor: int) -> str:
    """Render minor units as a fixed two-decimal string, without going through float."""
    sign = "-" if amount_minor < 0 else ""
    whole, frac = divmod(abs(amount_minor), 100)
    return f"{sign}{whole}.{frac:02d}"
