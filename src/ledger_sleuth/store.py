"""Read-only access to a generated ledger.

The connection is opened with ``mode=ro`` and ``query_only``, so the write path to the ledger
does not exist at the SQLite layer. Everything is loaded once and cached: rules and tools work on
immutable records, not on the connection.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from ledger_sleuth.models import (
    ApprovalTier,
    BankRow,
    Counterparty,
    Employee,
    IbanChange,
    LedgerRow,
)

LEDGER_FILENAME = "ledger.sqlite"


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"no ledger at {path}; run `ledger-sleuth generate` first")
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


class LedgerStore:
    """Immutable view of one public data directory. It has no reference to the ground truth."""

    def __init__(self, public_dir: Path) -> None:
        self.path = Path(public_dir) / LEDGER_FILENAME
        self.conn = connect_read_only(self.path)

    def close(self) -> None:
        self.conn.close()

    def _select(self, sql: str) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql).fetchall()

    @cached_property
    def ledger_rows(self) -> tuple[LedgerRow, ...]:
        rows = self._select(
            "SELECT row_id, company_id, doc_no, doc_type, direction, counterparty_id, posting_ts,"
            " value_date, currency, amount_minor, fx_rate_micro, amount_try_minor, reference,"
            " description, account, posted_by, approved_by FROM ledger_rows ORDER BY row_id"
        )
        return tuple(
            LedgerRow(
                row_id=r[0],
                company_id=r[1],
                doc_no=r[2],
                doc_type=r[3],
                direction=r[4],
                counterparty_id=r[5],
                posting_ts=datetime.fromisoformat(r[6]),
                value_date=date.fromisoformat(r[7]),
                currency=r[8],
                amount_minor=r[9],
                fx_rate_micro=r[10],
                amount_try_minor=r[11],
                reference=r[12],
                description=r[13],
                account=r[14],
                posted_by=r[15],
                approved_by=r[16],
            )
            for r in rows
        )

    @cached_property
    def bank_rows(self) -> tuple[BankRow, ...]:
        rows = self._select(
            "SELECT bank_row_id, company_id, account_id, value_date, currency, amount_minor,"
            " counterparty_iban, channel, reference FROM bank_rows ORDER BY bank_row_id"
        )
        return tuple(
            BankRow(
                bank_row_id=r[0],
                company_id=r[1],
                account_id=r[2],
                value_date=date.fromisoformat(r[3]),
                currency=r[4],
                amount_minor=r[5],
                counterparty_iban=r[6],
                channel=r[7],
                reference=r[8],
            )
            for r in rows
        )

    @cached_property
    def counterparties(self) -> dict[str, Counterparty]:
        rows = self._select(
            "SELECT counterparty_id, name, kind, vendor_type, currency, created_on, iban, address,"
            " company_ids FROM counterparties ORDER BY counterparty_id"
        )
        return {
            r[0]: Counterparty(
                counterparty_id=r[0],
                name=r[1],
                kind=r[2],
                vendor_type=r[3],
                currency=r[4],
                created_on=date.fromisoformat(r[5]),
                iban=r[6],
                address=r[7],
                company_ids=tuple(r[8].split(",")),
            )
            for r in rows
        }

    @cached_property
    def iban_changes(self) -> tuple[IbanChange, ...]:
        rows = self._select(
            "SELECT change_id, counterparty_id, changed_on, old_iban, new_iban, changed_by,"
            " verified_by FROM iban_changes ORDER BY change_id"
        )
        return tuple(
            IbanChange(r[0], r[1], date.fromisoformat(r[2]), r[3], r[4], r[5], r[6]) for r in rows
        )

    @cached_property
    def employees(self) -> tuple[Employee, ...]:
        rows = self._select(
            "SELECT employee_id, name, role, company_id, iban, address FROM employees"
            " ORDER BY employee_id"
        )
        return tuple(Employee(*r) for r in rows)

    @cached_property
    def approval_matrix(self) -> tuple[ApprovalTier, ...]:
        rows = self._select(
            "SELECT company_id, tier, max_amount_try_minor, approver_role FROM approval_matrix"
            " ORDER BY company_id, tier"
        )
        return tuple(ApprovalTier(*r) for r in rows)

    @cached_property
    def company_ids(self) -> tuple[str, ...]:
        return tuple(r[0] for r in self._select("SELECT company_id FROM companies ORDER BY 1"))

    @cached_property
    def ledger_by_id(self) -> dict[str, LedgerRow]:
        return {r.row_id: r for r in self.ledger_rows}

    @cached_property
    def bank_by_id(self) -> dict[str, BankRow]:
        return {r.bank_row_id: r for r in self.bank_rows}

    @cached_property
    def rows_by_counterparty(self) -> dict[str, list[LedgerRow]]:
        grouped: dict[str, list[LedgerRow]] = defaultdict(list)
        for row in self.ledger_rows:
            if row.counterparty_id is not None:
                grouped[row.counterparty_id].append(row)
        return dict(grouped)

    @cached_property
    def changes_by_counterparty(self) -> dict[str, list[IbanChange]]:
        grouped: dict[str, list[IbanChange]] = defaultdict(list)
        for change in self.iban_changes:
            grouped[change.counterparty_id].append(change)
        return dict(grouped)

    def limits(self, company_id: str) -> list[int]:
        """Finite approval limits of one company, ascending, in TRY minor units."""
        return sorted(
            t.max_amount_try_minor
            for t in self.approval_matrix
            if t.company_id == company_id and t.max_amount_try_minor is not None
        )
