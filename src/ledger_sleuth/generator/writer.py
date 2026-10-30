"""Assigns public identifiers and writes the visible ledger and the hidden ground truth."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ledger_sleuth.generator.world import APPROVAL_LIMITS_TRY, APPROVER_ROLES, COMPANIES, World
from ledger_sleuth.store import LEDGER_FILENAME

SCHEMA = """
CREATE TABLE companies (
    company_id TEXT PRIMARY KEY, name TEXT NOT NULL, base_currency TEXT NOT NULL);
CREATE TABLE counterparties (
    counterparty_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
    vendor_type TEXT NOT NULL, currency TEXT NOT NULL, created_on TEXT NOT NULL,
    iban TEXT NOT NULL, address TEXT NOT NULL, company_ids TEXT NOT NULL);
CREATE TABLE iban_changes (
    change_id TEXT PRIMARY KEY, counterparty_id TEXT NOT NULL, changed_on TEXT NOT NULL,
    old_iban TEXT NOT NULL, new_iban TEXT NOT NULL, changed_by TEXT NOT NULL, verified_by TEXT);
CREATE TABLE employees (
    employee_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, company_id TEXT NOT NULL,
    iban TEXT NOT NULL, address TEXT NOT NULL);
CREATE TABLE approval_matrix (
    company_id TEXT NOT NULL, tier INTEGER NOT NULL, max_amount_try_minor INTEGER,
    approver_role TEXT NOT NULL, PRIMARY KEY (company_id, tier));
CREATE TABLE ledger_rows (
    row_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, doc_no TEXT NOT NULL, doc_type TEXT NOT NULL,
    direction TEXT NOT NULL, counterparty_id TEXT, posting_ts TEXT NOT NULL,
    value_date TEXT NOT NULL, currency TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    fx_rate_micro INTEGER NOT NULL, amount_try_minor INTEGER NOT NULL, reference TEXT NOT NULL,
    description TEXT NOT NULL, account TEXT NOT NULL, posted_by TEXT NOT NULL, approved_by TEXT);
CREATE TABLE bank_rows (
    bank_row_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, account_id TEXT NOT NULL,
    value_date TEXT NOT NULL, currency TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    counterparty_iban TEXT NOT NULL, channel TEXT NOT NULL, reference TEXT NOT NULL);
"""


def public_dir(root: Path) -> Path:
    return root / "public"


def hidden_dir(root: Path) -> Path:
    return root / "hidden"


GROUND_TRUTH_FILENAME = "ground_truth.json"


def write_world(world: World, root: Path) -> dict[str, Any]:
    pub, hid = public_dir(root), hidden_dir(root)
    pub.mkdir(parents=True, exist_ok=True)
    hid.mkdir(parents=True, exist_ok=True)
    db_path = pub / LEDGER_FILENAME
    if db_path.exists():
        os.chmod(db_path, 0o644)
        db_path.unlink()

    rng = world.rng("final-order")
    ordered = sorted(world.rows, key=lambda r: (r.posting_ts, rng.random()))
    row_id = {r.uid: f"L-{i:06d}" for i, r in enumerate(ordered, start=1)}
    ordered_bank = sorted(world.bank, key=lambda b: (b.value_date, rng.random()))
    bank_id = {b.uid: f"B-{i:06d}" for i, b in enumerate(ordered_bank, start=1)}

    doc_counters: dict[tuple[str, str], int] = {}
    ledger_values: list[tuple[Any, ...]] = []
    for row in ordered:
        key = (row.company_id, row.doc_type)
        doc_counters[key] = doc_counters.get(key, 0) + 1
        ledger_values.append(
            (
                row_id[row.uid],
                row.company_id,
                f"{row.company_id}-{row.doc_type[:3]}-{doc_counters[key]:06d}",
                row.doc_type,
                row.direction,
                row.counterparty_id,
                row.posting_ts.isoformat(),
                row.value_date.isoformat(),
                row.currency,
                row.amount_minor,
                row.fx_rate_micro,
                row.amount_try_minor,
                row.reference,
                row.description,
                row.account,
                row.posted_by,
                row.approved_by,
            )
        )
    bank_values = [
        (
            bank_id[b.uid],
            b.company_id,
            b.account_id,
            b.value_date.isoformat(),
            b.currency,
            b.amount_minor,
            b.counterparty_iban,
            b.channel,
            b.reference,
        )
        for b in ordered_bank
    ]
    all_cps = sorted([*world.vendors, *world.customers], key=lambda c: c.counterparty_id)
    cp_values = [
        (
            c.counterparty_id,
            c.name,
            c.kind,
            c.vendor_type,
            c.currency,
            c.created_on.isoformat(),
            c.iban,
            c.address,
            ",".join(c.company_ids),
        )
        for c in all_cps
    ]
    changes = sorted(world.iban_changes, key=lambda c: (c.changed_on, c.counterparty_id))
    change_values = [
        (
            f"IC-{i:04d}",
            c.counterparty_id,
            c.changed_on.isoformat(),
            c.old_iban,
            c.new_iban,
            c.changed_by,
            c.verified_by,
        )
        for i, c in enumerate(changes, start=1)
    ]
    employee_values = sorted(
        (e.employee_id, e.name, e.role, e.company_id, e.iban, e.address) for e in world.employees
    )
    matrix_values = []
    for company_id, limits in APPROVAL_LIMITS_TRY.items():
        for tier, role in enumerate(APPROVER_ROLES, start=1):
            limit = limits[tier - 1] * 100 if tier <= len(limits) else None
            matrix_values.append((company_id, tier, limit, role))

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO companies VALUES (?,?,?)", COMPANIES)
    conn.executemany("INSERT INTO counterparties VALUES (?,?,?,?,?,?,?,?,?)", cp_values)
    conn.executemany("INSERT INTO iban_changes VALUES (?,?,?,?,?,?,?)", change_values)
    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?)", employee_values)
    conn.executemany("INSERT INTO approval_matrix VALUES (?,?,?,?)", matrix_values)
    conn.executemany(
        "INSERT INTO ledger_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ledger_values
    )
    conn.executemany("INSERT INTO bank_rows VALUES (?,?,?,?,?,?,?,?,?)", bank_values)
    conn.commit()
    conn.close()
    os.chmod(db_path, 0o444)

    digest = hashlib.sha256()
    for values in (ledger_values, bank_values, cp_values, change_values, employee_values):
        digest.update(json.dumps(values, ensure_ascii=True).encode())

    truth = {
        "seed": world.config.seed,
        "data_digest": digest.hexdigest(),
        "ledger_rows": len(ledger_values),
        "bank_rows": len(bank_values),
        "counterparties": len(cp_values),
        "incidents": [
            {
                "incident_id": f"INC-{i:03d}",
                "typology": inc.typology,
                "variant": inc.variant,
                "row_ids": [row_id[u] for u in inc.row_uids],
                "counterparty_id": inc.counterparty_id,
                "company_id": inc.company_id,
            }
            for i, inc in enumerate(world.incidents, start=1)
        ],
        "benign": [
            {
                "item_id": f"BEN-{i:04d}",
                "kind": item.kind,
                "variant": item.variant,
                "row_ids": [row_id[u] for u in item.row_uids],
                "bank_row_ids": [bank_id[u] for u in item.bank_uids],
            }
            for i, item in enumerate(world.benign, start=1)
        ],
        "decoys": [
            {
                "kind": decoy.kind,
                "counterparty_id": decoy.counterparty_id,
                "row_ids": [row_id[u] for u in decoy.row_uids],
            }
            for decoy in world.decoys
        ],
    }
    (hid / GROUND_TRUTH_FILENAME).write_text(json.dumps(truth, indent=1, sort_keys=True) + "\n")
    return truth
