"""Tolerance-based auto-closure: what a reconciliation team would script without any judgement."""

from __future__ import annotations

from dataclasses import dataclass

from ledger_sleuth.matcher import MatchResult, OpenItem

MAX_TIMING_GAP_DAYS = 7
MAX_FX_DIFF_BPS = 100
MIN_REFERENCE_SIMILARITY_PCT = 85


@dataclass(frozen=True, slots=True)
class ClosureDecision:
    item_id: str
    ledger_row_id: str | None
    reason: str


def _reason(item: OpenItem) -> str | None:
    metrics = item.metrics
    if item.kind == "timing_difference" and int(metrics["gap_days"]) <= MAX_TIMING_GAP_DAYS:
        return "timing within tolerance"
    if item.kind == "fx_difference" and abs(int(metrics["diff_bps"])) <= MAX_FX_DIFF_BPS:
        return "fx within tolerance"
    if item.kind == "partial_payment" and int(metrics["shortfall_minor"]) == 0:
        return "tranches sum to ledger amount"
    if (
        item.kind == "reference_mismatch"
        and int(metrics["similarity_pct"]) >= MIN_REFERENCE_SIMILARITY_PCT
    ):
        return "reference near-identical, amount exact"
    return None


def baseline_closures(result: MatchResult) -> list[ClosureDecision]:
    decisions = []
    for item in result.open_items:
        reason = _reason(item)
        if reason is not None:
            decisions.append(ClosureDecision(item.item_id, item.ledger_row_id, reason))
    return decisions
