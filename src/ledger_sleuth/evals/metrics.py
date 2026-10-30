"""Scores predictions against the hidden labels. Knows nothing about how they were produced."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledger_sleuth.generator.config import BENIGN_KINDS, TYPOLOGIES

# Typologies decided at vendor level: a finding on the right vendor is a hit.
COUNTERPARTY_LEVEL = frozenset({"benford_deviation", "employee_vendor_overlap"})
# Typologies that are a relation between rows: naming one row of the set could be coincidence.
MIN_ROW_OVERLAP = {"duplicate_payment": 2, "invoice_split": 2}


@dataclass(frozen=True, slots=True)
class Prediction:
    """One thing the system says is suspicious."""

    typology: str
    row_ids: tuple[str, ...]
    counterparty_id: str | None


@dataclass(frozen=True, slots=True)
class Closure:
    """One open item the system closes as benign."""

    item_id: str
    ledger_row_id: str | None


@dataclass(slots=True)
class TypologyScore:
    typology: str
    incidents: int = 0
    incidents_found: int = 0
    predictions: int = 0
    true_positives: int = 0
    false_alarms: int = 0

    @property
    def recall(self) -> float:
        return self.incidents_found / self.incidents if self.incidents else 0.0

    @property
    def precision(self) -> float:
        return self.true_positives / self.predictions if self.predictions else 0.0


@dataclass(slots=True)
class BenignScore:
    kind: str
    items: int = 0
    closed: int = 0
    flagged_as_fraud: int = 0

    @property
    def closure_rate(self) -> float:
        return self.closed / self.items if self.items else 0.0


@dataclass(slots=True)
class Scorecard:
    ledger_rows: int
    typologies: dict[str, TypologyScore]
    benign: dict[str, BenignScore]
    variants: dict[tuple[str, str], tuple[int, int]]
    wrong_closures: int = 0
    closures_total: int = 0

    def false_alarms_per_1k(self, typology: str | None = None) -> float:
        if typology is None:
            alarms = sum(t.false_alarms for t in self.typologies.values())
        else:
            alarms = self.typologies[typology].false_alarms
        return 1000 * alarms / self.ledger_rows

    @property
    def total(self) -> TypologyScore:
        summed = TypologyScore("TOTAL")
        for score in self.typologies.values():
            summed.incidents += score.incidents
            summed.incidents_found += score.incidents_found
            summed.predictions += score.predictions
            summed.true_positives += score.true_positives
            summed.false_alarms += score.false_alarms
        return summed

    @property
    def benign_total(self) -> BenignScore:
        summed = BenignScore("TOTAL")
        for score in self.benign.values():
            summed.items += score.items
            summed.closed += score.closed
            summed.flagged_as_fraud += score.flagged_as_fraud
        return summed


def load_truth(hidden_dir: Path) -> dict[str, Any]:
    truth: dict[str, Any] = json.loads((hidden_dir / "ground_truth.json").read_text())
    return truth


def _hits(prediction: Prediction, incident: dict[str, Any]) -> bool:
    if prediction.typology != incident["typology"]:
        return False
    if prediction.typology in COUNTERPARTY_LEVEL:
        return bool(prediction.counterparty_id == incident["counterparty_id"])
    overlap = set(prediction.row_ids) & set(incident["row_ids"])
    return len(overlap) >= MIN_ROW_OVERLAP.get(prediction.typology, 1)


def score(
    truth: dict[str, Any],
    predictions: Iterable[Prediction],
    closures: Iterable[Closure],
    *,
    scope_row_ids: set[str] | None = None,
) -> Scorecard:
    """Score against labels. ``scope_row_ids`` restricts them to a sampled part of the ledger."""
    predictions = list(predictions)
    closures = list(closures)
    incidents = truth["incidents"]
    benign = truth["benign"]
    if scope_row_ids is not None:
        incidents = [i for i in incidents if not scope_row_ids.isdisjoint(i["row_ids"])]
        benign = [b for b in benign if not scope_row_ids.isdisjoint(b["row_ids"])]

    typologies = {t: TypologyScore(t) for t in TYPOLOGIES}
    variant_totals: Counter[tuple[str, str]] = Counter()
    variant_found: Counter[tuple[str, str]] = Counter()

    by_typology: dict[str, list[dict[str, Any]]] = {t: [] for t in TYPOLOGIES}
    for incident in incidents:
        by_typology[incident["typology"]].append(incident)
        typologies[incident["typology"]].incidents += 1
        variant_totals[(incident["typology"], incident["variant"])] += 1

    found: set[str] = set()
    for prediction in predictions:
        card = typologies[prediction.typology]
        card.predictions += 1
        hit_any = False
        for incident in by_typology[prediction.typology]:
            if _hits(prediction, incident):
                hit_any = True
                found.add(incident["incident_id"])
        if hit_any:
            card.true_positives += 1
        else:
            card.false_alarms += 1
    for incident in incidents:
        if incident["incident_id"] in found:
            typologies[incident["typology"]].incidents_found += 1
            variant_found[(incident["typology"], incident["variant"])] += 1

    closed_rows = {c.ledger_row_id for c in closures if c.ledger_row_id is not None}
    flagged_rows = {row for p in predictions for row in p.row_ids}
    benign_scores = {k: BenignScore(k) for k in BENIGN_KINDS}
    for item in benign:
        card_b = benign_scores[item["kind"]]
        card_b.items += 1
        if not closed_rows.isdisjoint(item["row_ids"]):
            card_b.closed += 1
        if not flagged_rows.isdisjoint(item["row_ids"]):
            card_b.flagged_as_fraud += 1

    incident_rows = {row for i in truth["incidents"] for row in i["row_ids"]}
    return Scorecard(
        ledger_rows=truth["ledger_rows"],
        typologies=typologies,
        benign=benign_scores,
        variants={key: (variant_found[key], total) for key, total in variant_totals.items()},
        wrong_closures=sum(1 for c in closures if c.ledger_row_id in incident_rows),
        closures_total=len(closures),
    )


def render(card: Scorecard, title: str) -> str:
    lines = [f"== {title} ==", f"ledger rows: {card.ledger_rows}", ""]
    lines.append(
        f"{'typology':<26}{'incidents':>10}{'found':>7}{'recall':>8}"
        f"{'findings':>10}{'TP':>6}{'precision':>11}{'FA/1k rows':>12}"
    )
    rows = [*card.typologies.values(), card.total]
    for row in rows:
        per_1k = card.false_alarms_per_1k(None if row.typology == "TOTAL" else row.typology)
        lines.append(
            f"{row.typology:<26}{row.incidents:>10}{row.incidents_found:>7}{row.recall:>8.2f}"
            f"{row.predictions:>10}{row.true_positives:>6}{row.precision:>11.3f}{per_1k:>12.3f}"
        )
    lines += ["", f"{'benign noise':<26}{'items':>10}{'closed':>8}{'closure':>9}{'flagged':>9}"]
    for row_b in [*card.benign.values(), card.benign_total]:
        lines.append(
            f"{row_b.kind:<26}{row_b.items:>10}{row_b.closed:>8}"
            f"{row_b.closure_rate:>9.2f}{row_b.flagged_as_fraud:>9}"
        )
    lines += [
        "",
        f"closures total: {card.closures_total}; "
        f"closures on a row of a labelled incident: {card.wrong_closures}",
        "",
        f"{'recall by variant':<52}{'found':>7}{'of':>5}",
    ]
    for (typology, variant), (found, total) in sorted(card.variants.items()):
        lines.append(f"{typology + ' / ' + variant:<52}{found:>7}{total:>5}")
    return "\n".join(lines)
