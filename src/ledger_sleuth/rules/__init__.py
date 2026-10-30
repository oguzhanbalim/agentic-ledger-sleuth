"""Transparent baseline detectors, one per typology.

They are a wide net with no model in the loop. Thresholds are module constants, each finding
carries the numbers behind it, and every rule can be read in a minute. They are recall-biased on
purpose: the agent exists to work through what they flag.
"""

from __future__ import annotations

from ledger_sleuth.rules.closure import ClosureDecision, baseline_closures
from ledger_sleuth.rules.detectors import ALL_DETECTORS, Finding, run_all_rules

__all__ = ["ALL_DETECTORS", "ClosureDecision", "Finding", "baseline_closures", "run_all_rules"]
