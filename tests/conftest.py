from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ledger_sleuth.generator import TRAIN_SEED, GeneratorConfig, generate, hidden_dir, public_dir
from ledger_sleuth.store import LedgerStore
from ledger_sleuth.tools import LedgerTools, PendingQueue


@pytest.fixture(scope="session")
def dataset_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("data") / f"seed-{TRAIN_SEED}"
    generate(GeneratorConfig(seed=TRAIN_SEED), root)
    return root


@pytest.fixture(scope="session")
def truth(dataset_root: Path) -> dict[str, Any]:
    from ledger_sleuth.evals.metrics import load_truth

    return load_truth(hidden_dir(dataset_root))


@pytest.fixture(scope="session")
def store(dataset_root: Path) -> Iterator[LedgerStore]:
    opened = LedgerStore(public_dir(dataset_root))
    yield opened
    opened.close()


@pytest.fixture
def tools(store: LedgerStore, tmp_path: Path) -> LedgerTools:
    return LedgerTools(store, PendingQueue(tmp_path / "pending.jsonl"))
