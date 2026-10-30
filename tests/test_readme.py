"""The README may only quote numbers a script printed. These tests hold it to that."""

from __future__ import annotations

import re
from pathlib import Path

from ledger_sleuth.evals.baseline import baseline_report
from ledger_sleuth.generator import HELDOUT_SEED, TRAIN_SEED

README = Path(__file__).resolve().parents[1] / "README.md"
MARKER = "<!-- baseline-output -->"


def _quoted_blocks(text: str) -> list[list[str]]:
    blocks = re.findall(re.escape(MARKER) + r"\n```text\n(.*?)\n```", text, flags=re.S)
    return [block.splitlines() for block in blocks]


def _is_subsequence(quoted: list[str], full: list[str]) -> bool:
    remaining = iter(full)
    return all(line in remaining for line in quoted)


def test_quoted_baseline_matches_a_fresh_run(tmp_path: Path) -> None:
    fresh = baseline_report(tmp_path, [TRAIN_SEED, HELDOUT_SEED]).splitlines()
    blocks = _quoted_blocks(README.read_text())
    assert len(blocks) == 2, "train and held-out blocks"
    for block in blocks:
        assert _is_subsequence(block, fresh), "README drifted from `make eval-baseline`"
    assert sum(1 for b in blocks for line in b if line.startswith("TOTAL")) >= 4


def test_readme_states_the_status_plainly() -> None:
    text = README.read_text()
    assert "Built on synthetic data, no customer data." in text
    assert "Not affiliated with Anthropic." in text
    assert "not yet run" in text
    assert "== agent ==" not in text


def test_readme_stays_plain() -> None:
    text = README.read_text()
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text), "no emoji"
    lowered = text.lower()
    banned = (
        "production-grade",
        "enterprise",
        "powerful",
        "seamless",
        "robust",
        "cutting-edge",
        "state-of-the-art",
        "blazing",
        "best-in-class",
        "job search",
        "resume",
        "hiring",
        "employer",
    )
    assert not [word for word in banned if word in lowered]
