"""Deterministic synthetic ERP: legitimate flows, labelled anomalies and benign noise."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ledger_sleuth.generator.anomalies import inject_all
from ledger_sleuth.generator.config import HELDOUT_SEED, TRAIN_SEED, GeneratorConfig
from ledger_sleuth.generator.noise import add_noise
from ledger_sleuth.generator.world import World
from ledger_sleuth.generator.writer import (
    GROUND_TRUTH_FILENAME,
    hidden_dir,
    public_dir,
    write_world,
)

__all__ = [
    "GROUND_TRUTH_FILENAME",
    "HELDOUT_SEED",
    "TRAIN_SEED",
    "GeneratorConfig",
    "generate",
    "hidden_dir",
    "public_dir",
]


def generate(config: GeneratorConfig, root: Path) -> dict[str, Any]:
    """Build one dataset under ``root`` and return its manifest and labels."""
    world = World(config)
    world.build_legit_activity()
    inject_all(world)
    add_noise(world)
    return write_world(world, root)
