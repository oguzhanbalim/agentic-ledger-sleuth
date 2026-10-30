from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from helpers import message, text
from ledger_sleuth import cli
from ledger_sleuth.evals.agent_eval import evaluate_agent, sample_clusters
from ledger_sleuth.evals.baseline import evaluate_baseline
from ledger_sleuth.evals.metrics import Closure, Prediction, render, score


def _truth() -> dict[str, Any]:
    return {
        "ledger_rows": 1000,
        "incidents": [
            {
                "incident_id": "I1",
                "typology": "duplicate_payment",
                "variant": "same_ref_same_amount",
                "row_ids": ["L-1", "L-2"],
                "counterparty_id": "V-1",
            },
            {
                "incident_id": "I2",
                "typology": "benford_deviation",
                "variant": "strong_skew",
                "row_ids": ["L-3"],
                "counterparty_id": "V-2",
            },
            {
                "incident_id": "I3",
                "typology": "off_hours_posting",
                "variant": "night_manual",
                "row_ids": ["L-9"],
                "counterparty_id": "V-3",
            },
        ],
        "benign": [
            {"item_id": "B1", "kind": "timing_difference", "variant": "x", "row_ids": ["L-5"]},
            {"item_id": "B2", "kind": "typo_reference", "variant": "x", "row_ids": ["L-6"]},
        ],
    }


def test_duplicate_needs_both_rows_of_the_pair() -> None:
    one = score(_truth(), [Prediction("duplicate_payment", ("L-1", "L-7"), "V-1")], [])
    assert one.typologies["duplicate_payment"].true_positives == 0
    assert one.typologies["duplicate_payment"].false_alarms == 1
    both = score(_truth(), [Prediction("duplicate_payment", ("L-1", "L-2"), "V-1")], [])
    assert both.typologies["duplicate_payment"].incidents_found == 1


def test_vendor_level_typology_matches_on_vendor() -> None:
    card = score(_truth(), [Prediction("benford_deviation", ("L-99",), "V-2")], [])
    assert card.typologies["benford_deviation"].recall == 1.0


def test_right_rows_wrong_typology_is_a_false_alarm() -> None:
    card = score(_truth(), [Prediction("just_below_threshold", ("L-9",), "V-3")], [])
    assert card.typologies["just_below_threshold"].false_alarms == 1
    assert card.typologies["off_hours_posting"].incidents_found == 0


def test_precision_recall_and_rate() -> None:
    card = score(
        _truth(),
        [
            Prediction("off_hours_posting", ("L-9",), "V-3"),
            Prediction("off_hours_posting", ("L-50",), "V-3"),
        ],
        [],
    )
    typology = card.typologies["off_hours_posting"]
    assert typology.recall == 1.0
    assert typology.precision == 0.5
    assert card.false_alarms_per_1k("off_hours_posting") == 1.0


def test_benign_closure_and_flagging_and_wrong_closure() -> None:
    card = score(
        _truth(),
        [Prediction("off_hours_posting", ("L-6",), None)],
        [Closure("OI-L-5", "L-5"), Closure("OI-L-9", "L-9")],
    )
    assert card.benign["timing_difference"].closure_rate == 1.0
    assert card.benign["typo_reference"].closure_rate == 0.0
    assert card.benign["typo_reference"].flagged_as_fraud == 1
    assert card.wrong_closures == 1


def test_scope_restricts_labels() -> None:
    card = score(_truth(), [], [], scope_row_ids={"L-9"})
    assert card.total.incidents == 1
    assert card.benign_total.items == 0


def test_baseline_runs_offline_on_a_full_dataset(dataset_root: Path) -> None:
    card = evaluate_baseline(dataset_root)
    total = card.total
    assert 0 < total.incidents_found <= total.incidents
    assert total.predictions == total.true_positives + total.false_alarms
    assert "rules" in render(card, "rules test")


def test_baseline_has_measured_blind_spots(dataset_root: Path) -> None:
    """These variants sit outside what the rules can see by construction."""
    card = evaluate_baseline(dataset_root)
    for key in (
        ("duplicate_payment", "net_of_fee"),
        ("just_below_threshold", "wide_band"),
        ("off_hours_posting", "holiday_business_hours"),
        ("new_vendor_high_value", "aged_shell"),
        ("employee_vendor_overlap", "address_abbreviated"),
    ):
        found, total = card.variants[key]
        assert total > 0
        assert found == 0, key


class PolicyMessages:
    """Answers any cluster, closing everything as benign. Exercises the harness only."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls = 0

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.refuse:
            return message(
                [], stop_reason="refusal", stop_details={"type": "refusal", "category": None}
            )
        if "reviewer" in kwargs["system"][0]["text"]:
            return message([text({"verdict": "accept", "objections": []})])
        brief_text = kwargs["messages"][0]["content"].split("\n\n", 1)[1]
        brief = json.loads(brief_text)
        rows = [r for f in brief["findings"] for r in f["row_ids"]]
        rows += [i["ledger_row_id"] for i in brief["open_items"] if i["ledger_row_id"]]
        rows += [b for i in brief["open_items"] for b in i["bank_row_ids"]]
        claim = {
            "statement": "Scripted test claim.",
            "row_ids": rows,
            "finding_ids": [f["finding_id"] for f in brief["findings"]],
            "amount": None,
            "date": None,
        }
        return message(
            [
                text(
                    {
                        "cluster_id": brief["cluster_id"],
                        "disposition": "benign",
                        "summary": "Scripted.",
                        "claims": [claim],
                        "finding_dispositions": [
                            {
                                "finding_ids": [f["finding_id"] for f in brief["findings"]],
                                "disposition": "benign",
                                "reason": "Scripted.",
                            }
                        ],
                        "open_item_dispositions": [
                            {
                                "item_ids": [i["item_id"] for i in brief["open_items"]],
                                "disposition": "close_benign",
                                "reason": "Scripted.",
                            }
                        ],
                        "adjustment_draft_ids": [],
                        "recommended_action": "None.",
                    }
                )
            ]
        )


class PolicyClient:
    def __init__(self, *, refuse: bool = False) -> None:
        self.messages = PolicyMessages(refuse=refuse)


def test_agent_harness_counts_only_accepted_closures(dataset_root: Path, tmp_path: Path) -> None:
    """A scripted client that closes everything: harness mechanics, not agent quality."""
    result = evaluate_agent(
        dataset_root, PolicyClient(), max_clusters=8, seed=1, run_dir=tmp_path / "run"
    )
    assert result.clusters_run == 8
    assert all(o.status == "accepted" for o in result.outcomes)
    assert result.agent.total.predictions == 0
    assert result.baseline_on_sample.total.predictions > 0
    assert (tmp_path / "run" / "outcomes.jsonl").exists()


def test_failed_agent_run_degrades_to_the_rules_answer(dataset_root: Path, tmp_path: Path) -> None:
    result = evaluate_agent(
        dataset_root, PolicyClient(refuse=True), max_clusters=8, seed=1, run_dir=tmp_path / "run"
    )
    assert all(o.status == "refused" for o in result.outcomes)
    assert result.agent.total.predictions == result.baseline_on_sample.total.predictions
    assert result.agent.closures_total == 0


def test_cluster_sample_is_seeded_and_bounded() -> None:
    items: list[Any] = list(range(100))
    assert sample_clusters(items, 10, 1) == sample_clusters(items, 10, 1)
    assert sample_clusters(items, 10, 1) != sample_clusters(items, 10, 2)
    assert sample_clusters(items, 200, 1) == items


@pytest.mark.parametrize("command", ["investigate", "eval-agent"])
def test_live_commands_refuse_cleanly_without_a_key(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = cli.main(["--data-root", str(tmp_path), command])
    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
