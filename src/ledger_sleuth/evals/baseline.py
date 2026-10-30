"""Rules-baseline evaluation. Offline: no model, no network, no key."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ledger_sleuth.evals.metrics import Closure, Prediction, Scorecard, load_truth, render, score
from ledger_sleuth.generator import (
    HELDOUT_SEED,
    TRAIN_SEED,
    GeneratorConfig,
    generate,
    hidden_dir,
    public_dir,
)
from ledger_sleuth.matcher import run_matcher
from ledger_sleuth.rules import baseline_closures, run_all_rules
from ledger_sleuth.store import LEDGER_FILENAME, LedgerStore


def dataset_root(data_root: Path, seed: int) -> Path:
    return data_root / f"seed-{seed}"


def ensure_dataset(data_root: Path, seed: int) -> Path:
    root = dataset_root(data_root, seed)
    if not (public_dir(root) / LEDGER_FILENAME).exists():
        generate(GeneratorConfig(seed=seed), root)
    return root


def evaluate_baseline(root: Path) -> Scorecard:
    store = LedgerStore(public_dir(root))
    try:
        predictions = [
            Prediction(f.typology, f.row_ids, f.counterparty_id) for f in run_all_rules(store)
        ]
        closures = [
            Closure(d.item_id, d.ledger_row_id) for d in baseline_closures(run_matcher(store))
        ]
    finally:
        store.close()
    return score(load_truth(hidden_dir(root)), predictions, closures)


def _label(seed: int) -> str:
    return {TRAIN_SEED: "train", HELDOUT_SEED: "held-out"}.get(seed, "custom")


def baseline_report(data_root: Path, seeds: Iterable[int]) -> str:
    """The text the README quotes. A test re-runs this and compares it with the README."""
    sections = []
    for seed in seeds:
        card = evaluate_baseline(ensure_dataset(data_root, seed))
        sections.append(render(card, f"rules baseline, {_label(seed)} seed {seed}"))
    return "\n\n".join(sections)
