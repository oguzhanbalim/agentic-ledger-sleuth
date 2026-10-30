from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from ledger_sleuth.generator import (
    HELDOUT_SEED,
    TRAIN_SEED,
    GeneratorConfig,
    generate,
    public_dir,
)
from ledger_sleuth.generator.config import BENIGN_KINDS, TYPOLOGIES
from ledger_sleuth.matcher import run_matcher
from ledger_sleuth.store import LEDGER_FILENAME, LedgerStore


def test_shape_matches_the_brief(store: LedgerStore, truth: dict[str, Any]) -> None:
    assert 55_000 <= len(store.ledger_rows) <= 65_000
    assert 180 <= len(store.counterparties) <= 230
    assert {r.company_id for r in store.ledger_rows} == {"CO-A", "CO-B", "CO-C"}
    assert {r.currency for r in store.ledger_rows} == {"TRY", "EUR", "USD"}
    months = {(r.value_date.year, r.value_date.month) for r in store.ledger_rows}
    assert {(2025, m) for m in range(1, 13)} <= months
    assert len(store.bank_rows) > 20_000


def test_every_typology_and_benign_kind_is_present(truth: dict[str, Any]) -> None:
    assert {i["typology"] for i in truth["incidents"]} == set(TYPOLOGIES)
    assert {b["kind"] for b in truth["benign"]} == set(BENIGN_KINDS)


def test_same_seed_is_identical_and_seeds_differ(tmp_path: Path) -> None:
    first = generate(GeneratorConfig(seed=TRAIN_SEED, scale=0.1), tmp_path / "a")
    second = generate(GeneratorConfig(seed=TRAIN_SEED, scale=0.1), tmp_path / "b")
    other = generate(GeneratorConfig(seed=HELDOUT_SEED, scale=0.1), tmp_path / "c")
    assert first == second
    assert first["data_digest"] != other["data_digest"]


def test_regeneration_over_a_read_only_ledger_works(tmp_path: Path) -> None:
    generate(GeneratorConfig(seed=TRAIN_SEED, scale=0.05), tmp_path)
    generate(GeneratorConfig(seed=TRAIN_SEED, scale=0.05), tmp_path)


def test_labels_are_not_in_the_visible_data(dataset_root: Path) -> None:
    pub = public_dir(dataset_root)
    assert [p.name for p in pub.iterdir()] == [LEDGER_FILENAME]
    conn = sqlite3.connect(pub / LEDGER_FILENAME)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    forbidden = ("anomal", "fraud", "label", "typolog", "incident", "decoy", "shell", "variant")
    for table in tables:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert not [c for c in columns if any(word in c.lower() for word in forbidden)]
        for row in conn.execute(f"SELECT * FROM {table} LIMIT 5000"):
            joined = " ".join(str(v) for v in row).lower()
            assert not any(word in joined for word in forbidden), row
    conn.close()


def test_row_ids_do_not_reveal_injection(store: LedgerStore, truth: dict[str, Any]) -> None:
    """IDs follow posting time, so injected rows must not cluster at the end of the sequence."""
    rank = {row.row_id: i for i, row in enumerate(store.ledger_rows)}
    incident_rows = [row for inc in truth["incidents"] for row in inc["row_ids"]]
    mean_rank = sum(rank[r] for r in incident_rows) / len(incident_rows) / len(rank)
    assert 0.3 < mean_rank < 0.7


def test_all_ibans_use_the_unassigned_country_code(store: LedgerStore) -> None:
    ibans = [c.iban for c in store.counterparties.values()] + [e.iban for e in store.employees]
    ibans += [b.counterparty_iban for b in store.bank_rows]
    assert all(iban.startswith("XX") for iban in ibans)


def test_every_benign_item_surfaces_as_an_open_item(
    store: LedgerStore, truth: dict[str, Any]
) -> None:
    open_rows = {i.ledger_row_id: i.kind for i in run_matcher(store).open_items}
    expected = {
        "timing_difference": {"timing_difference"},
        "fx_difference": {"fx_difference", "amount_mismatch"},
        "partial_payment": {"partial_payment"},
        "typo_reference": {"reference_mismatch", "unmatched_ledger"},
        "refund": {"reference_mismatch", "unmatched_ledger"},
    }
    missing: Counter[str] = Counter()
    for item in truth["benign"]:
        kinds = {open_rows[r] for r in item["row_ids"] if r in open_rows}
        if not kinds & expected[item["kind"]]:
            missing[item["kind"]] += 1
    assert not missing


def test_store_cannot_write(store: LedgerStore) -> None:
    import pytest

    with pytest.raises(sqlite3.OperationalError):
        store.conn.execute("DELETE FROM ledger_rows")
    with pytest.raises(sqlite3.OperationalError):
        store.conn.execute("UPDATE ledger_rows SET amount_minor = 0")
