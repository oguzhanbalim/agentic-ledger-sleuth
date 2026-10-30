from __future__ import annotations

import math
from pathlib import Path

from helpers import build_store, counterparty, ledger_row
from ledger_sleuth.matcher import MatchResult, OpenItem, reference_similarity_pct, run_matcher
from ledger_sleuth.rules import baseline_closures, run_all_rules
from ledger_sleuth.rules import detectors as d
from ledger_sleuth.store import LedgerStore


def test_duplicate_within_window_is_found_and_outside_is_not(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-1", doc_type="PAYMENT", ts="2025-03-01T10:00:00", amount=5_000_00),
            ledger_row("L-2", doc_type="PAYMENT", ts="2025-03-15T10:00:00", amount=5_000_00),
            ledger_row("L-3", doc_type="PAYMENT", ts="2025-05-15T10:00:00", amount=5_000_00),
        ],
    )
    findings = d.detect_duplicate_payments(store)
    assert [f.row_ids for f in findings] == [("L-1", "L-2")]
    assert findings[0].metrics["gap_days"] == 14


def test_duplicate_same_reference_ignores_the_window(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-1", doc_type="PAYMENT", ts="2025-01-01T10:00:00", reference="INV-9"),
            ledger_row("L-2", doc_type="PAYMENT", ts="2025-06-01T10:00:00", reference="inv 9"),
        ],
    )
    assert len(d.detect_duplicate_payments(store)) == 1


def test_split_needs_parts_under_the_limit_summing_over_it(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-1", ts="2025-03-01T10:00:00", amount=60_000_00),
            ledger_row("L-2", ts="2025-03-05T10:00:00", amount=50_000_00),
            ledger_row("L-3", ts="2025-06-01T10:00:00", amount=60_000_00),
            ledger_row("L-4", ts="2025-09-01T10:00:00", amount=60_000_00),
            ledger_row("L-5", ts="2025-09-08T10:00:00", amount=30_000_00),
        ],
    )
    findings = d.detect_invoice_splits(store)
    assert [f.row_ids for f in findings] == [("L-1", "L-2")]
    assert findings[0].metrics["total_try_minor"] == 110_000_00


def test_split_window_is_anchored_not_chained(tmp_path: Path) -> None:
    """A steady stream of invoices must not merge into one endless burst."""
    rows = [
        ledger_row(f"L-{i}", ts=f"2025-03-{1 + 5 * i:02d}T10:00:00", amount=60_000_00)
        for i in range(5)
    ]
    store = build_store(tmp_path, counterparties=[counterparty()], ledger_rows=rows)
    assert all(len(f.row_ids) == 2 for f in d.detect_invoice_splits(store))


def test_split_is_not_lost_when_a_window_straddles_it(tmp_path: Path) -> None:
    """The first window (30k + 60k) stays under the limit; the next one (60k + 50k) does not."""
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-1", ts="2025-03-01T10:00:00", amount=30_000_00),
            ledger_row("L-2", ts="2025-03-07T10:00:00", amount=60_000_00),
            ledger_row("L-3", ts="2025-03-13T10:00:00", amount=50_000_00),
        ],
    )
    assert [f.row_ids for f in d.detect_invoice_splits(store)] == [("L-2", "L-3")]


def test_bank_change_reports_verification_rather_than_trusting_it(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        iban_changes=[("IC-1", "V-1", "2025-03-01", "XX1", "XX2", "E-1", "E-2")],
        ledger_rows=[
            ledger_row("L-1", doc_type="PAYMENT", ts="2025-03-10T10:00:00"),
            ledger_row("L-2", doc_type="PAYMENT", ts="2025-04-10T10:00:00"),
        ],
    )
    findings = d.detect_bank_detail_changes(store)
    assert [f.row_ids for f in findings] == [("L-1",)]
    assert findings[0].metrics["verified_by"] == "E-2"
    assert findings[0].metrics["days_to_first_payment"] == 9


def test_just_below_band_boundaries(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-in", amount=99_000_00),
            ledger_row("L-edge", amount=98_000_00),
            ledger_row("L-out", amount=97_000_00),
            ledger_row("L-limit", amount=100_000_00),
            ledger_row("L-pay", doc_type="PAYMENT", amount=99_000_00),
        ],
    )
    assert {f.row_ids[0] for f in d.detect_just_below_threshold(store)} == {"L-in", "L-edge"}


def test_off_hours_exempts_batch_but_not_close(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-night", ts="2025-03-12T02:00:00"),
            ledger_row("L-day", ts="2025-03-12T11:00:00"),
            ledger_row("L-batch", ts="2025-03-12T02:00:00", posted_by="SYS-BATCH"),
            ledger_row("L-sat", ts="2025-03-15T11:00:00"),
            ledger_row("L-close", ts="2025-03-31T22:00:00"),
        ],
    )
    findings = {f.row_ids[0]: f for f in d.detect_off_hours(store)}
    assert set(findings) == {"L-night", "L-sat", "L-close"}
    assert findings["L-close"].metrics["in_close_window"] == "true"
    assert findings["L-night"].metrics["in_close_window"] == "false"


def test_new_vendor_needs_both_youth_and_size(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        counterparties=[counterparty("V-1", created="2025-03-01"), counterparty("V-2")],
        ledger_rows=[
            ledger_row("L-1", doc_type="PAYMENT", ts="2025-03-20T10:00:00", amount=300_000_00),
            ledger_row("L-2", doc_type="PAYMENT", ts="2025-03-21T10:00:00", amount=100_000_00),
            ledger_row(
                "L-3",
                doc_type="PAYMENT",
                ts="2025-03-21T10:00:00",
                amount=300_000_00,
                counterparty="V-2",
            ),
        ],
    )
    assert [f.row_ids for f in d.detect_new_vendor_high_value(store)] == [("L-1",)]


def test_benford_statistics_are_near_zero_on_a_conforming_distribution() -> None:
    amounts = []
    for digit, count in zip(range(1, 10), (301, 176, 125, 97, 79, 67, 58, 51, 46), strict=True):
        amounts += [digit * 100] * count
    chi2, mad = d.benford_statistics(amounts)
    assert chi2 < 0.1
    assert mad < 0.001
    assert math.isclose(sum(d.BENFORD_EXPECTED), 1.0)


def test_benford_flags_skew_and_skips_small_samples(tmp_path: Path) -> None:
    skewed = [
        ledger_row(f"L-{i}", ts="2025-03-10T10:00:00", amount=(5 + i % 5) * 1000_00)
        for i in range(40)
    ]
    small = [
        ledger_row(f"M-{i}", ts="2025-03-10T10:00:00", amount=7000_00, counterparty="V-2")
        for i in range(29)
    ]
    store = build_store(
        tmp_path, counterparties=[counterparty(), counterparty("V-2")], ledger_rows=skewed + small
    )
    assert [f.counterparty_id for f in d.detect_benford_deviation(store)] == ["V-1"]


def test_overlap_normalises_iban_and_address_but_only_exact(tmp_path: Path) -> None:
    employee = ("E-1", "Synthetic Employee", "AP_CLERK", "CO-A", "XX991234", "Ardic Sokak No:1/1")
    store = build_store(
        tmp_path,
        employees=[employee],
        counterparties=[
            counterparty("V-1", iban="xx99 1234"),
            counterparty("V-2", iban="XX1", address="ARDIC SOKAK NO:1/1"),
            counterparty("V-3", iban="XX2", address="Ardic Sk. No 1/1"),
        ],
    )
    found = {
        f.counterparty_id: f.metrics["matched_field"]
        for f in d.detect_employee_vendor_overlap(store)
    }
    assert found == {"V-1": "iban", "V-2": "address"}


def test_finding_ids_are_stable(store: LedgerStore) -> None:
    first = [f.finding_id for f in run_all_rules(store)]
    assert first == [f.finding_id for f in run_all_rules(store)]
    assert len(first) == len(set(first))


def test_rule_metrics_hold_no_floats(store: LedgerStore) -> None:
    """Numbers shown to the model stay exact: integers or strings, never binary fractions."""
    for finding in run_all_rules(store):
        assert all(isinstance(v, int | str) for v in finding.metrics.values())


def test_matcher_uses_each_bank_row_once(store: LedgerStore) -> None:
    result = run_matcher(store)
    used = [b for ids in result.matched.values() for b in ids]
    used += [b for item in result.open_items for b in item.bank_row_ids]
    assert len(used) == len(set(used))
    assert set(used) == set(store.bank_by_id)


def test_matcher_classes_on_handwritten_rows(tmp_path: Path) -> None:
    def bank(bid: str, day: str, amount: int, ref: str, ccy: str = "TRY") -> tuple[object, ...]:
        return (bid, "CO-A", "BA-A-TRY", day, ccy, amount, "XX1", "EFT", ref)

    store = build_store(
        tmp_path,
        counterparties=[counterparty()],
        ledger_rows=[
            ledger_row("L-ok", doc_type="PAYMENT", ts="2025-03-01T10:00:00", reference="R1"),
            ledger_row("L-late", doc_type="PAYMENT", ts="2025-03-01T10:00:00", reference="R2"),
            ledger_row(
                "L-part",
                doc_type="PAYMENT",
                ts="2025-03-01T10:00:00",
                reference="R3",
                amount=300_00,
            ),
            ledger_row(
                "L-typo",
                doc_type="PAYMENT",
                ts="2025-03-01T10:00:00",
                reference="INV-1000-123456",
                amount=777_00,
            ),
            ledger_row("L-none", doc_type="PAYMENT", ts="2025-03-01T10:00:00", reference="R5"),
        ],
        bank_rows=[
            bank("B-1", "2025-03-02", -100_00, "R1"),
            bank("B-2", "2025-03-12", -100_00, "R2"),
            bank("B-3", "2025-03-02", -200_00, "R3"),
            bank("B-4", "2025-03-05", -100_00, "R3"),
            bank("B-5", "2025-03-01", -777_00, "INV-1000-132456"),
            bank("B-6", "2025-03-01", -999_00, "UNKNOWN"),
        ],
    )
    result = run_matcher(store)
    assert result.matched == {"L-ok": ("B-1",)}
    kinds = {i.ledger_row_id: i.kind for i in result.open_items}
    assert kinds == {
        "L-late": "timing_difference",
        "L-part": "partial_payment",
        "L-typo": "reference_mismatch",
        "L-none": "unmatched_ledger",
        None: "unmatched_bank",
    }
    partial = next(i for i in result.open_items if i.ledger_row_id == "L-part")
    assert partial.metrics["shortfall_minor"] == 0
    assert partial.metrics["tranches"] == 2


def test_reference_similarity() -> None:
    assert reference_similarity_pct("INV-1", "inv 1") == 100
    assert reference_similarity_pct("INV-1000-123456", "INV-1000-132456") > 85
    assert reference_similarity_pct("", "X") == 0


def _item(kind: str, **metrics: int | str) -> OpenItem:
    return OpenItem(f"OI-{kind}", kind, "CO-A", "L-1", (), None, metrics)


def test_tolerance_closures_at_their_boundaries() -> None:
    result = MatchResult(
        open_items=[
            _item("timing_difference", gap_days=7),
            _item("fx_difference", diff_bps=-100),
            _item("partial_payment", shortfall_minor=0),
            _item("reference_mismatch", similarity_pct=85),
        ]
    )
    assert len(baseline_closures(result)) == 4
    result = MatchResult(
        open_items=[
            _item("timing_difference", gap_days=8),
            _item("fx_difference", diff_bps=101),
            _item("partial_payment", shortfall_minor=1),
            _item("reference_mismatch", similarity_pct=84),
            _item("unmatched_ledger"),
        ]
    )
    assert baseline_closures(result) == []
