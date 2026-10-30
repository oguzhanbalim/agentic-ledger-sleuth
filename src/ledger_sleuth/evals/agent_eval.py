"""Live-agent evaluation over the same scorer as the baseline.

Spends tokens, needs a client, and only ever runs when asked. It scores a sample of clusters. The
paired baseline on the same sample is what makes the two comparable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from ledger_sleuth.agent.clusters import Cluster, build_clusters
from ledger_sleuth.agent.loop import ClaudeClient
from ledger_sleuth.agent.orchestrator import AgentConfig, CaseOutcome, Orchestrator, write_outcomes
from ledger_sleuth.agent.skills import Skill
from ledger_sleuth.evals.metrics import Closure, Prediction, Scorecard, load_truth, score
from ledger_sleuth.generator import hidden_dir, public_dir
from ledger_sleuth.matcher import MatchResult
from ledger_sleuth.rules import baseline_closures
from ledger_sleuth.store import LedgerStore
from ledger_sleuth.tools import PENDING_QUEUE_FILENAME, LedgerTools, PendingQueue


@dataclass(slots=True)
class AgentEvaluation:
    agent: Scorecard
    baseline_on_sample: Scorecard
    outcomes: list[CaseOutcome]
    clusters_available: int
    clusters_run: int


def sample_clusters(clusters: list[Cluster], max_clusters: int, seed: int) -> list[Cluster]:
    """A seeded random sample, not the riskiest N: precision on a top slice would not generalise."""
    if len(clusters) <= max_clusters:
        return clusters
    return random.Random(f"eval:{seed}").sample(clusters, max_clusters)


def outcomes_to_predictions(
    outcomes: list[CaseOutcome], clusters: list[Cluster], tools: LedgerTools
) -> tuple[list[Prediction], list[Closure]]:
    """What the system still says after the agent has run.

    A finding stays a prediction unless an accepted case closed it as benign. Refusals, gate
    rejections and escalations close nothing, so a failed run degrades to the rules' answer.
    """
    by_id = {o.cluster_id: o for o in outcomes}
    items = {item.item_id: item for item in tools.open_items}
    predictions: list[Prediction] = []
    closures: list[Closure] = []
    for cluster in clusters:
        outcome = by_id.get(cluster.cluster_id)
        case = outcome.case_file if outcome and outcome.status == "accepted" else None
        benign = {
            finding_id
            for group in (case.finding_dispositions if case else [])
            if group.disposition == "benign"
            for finding_id in group.finding_ids
        }
        for finding_id in cluster.finding_ids:
            if finding_id not in benign:
                finding = tools.finding(finding_id)
                if finding is not None:
                    predictions.append(
                        Prediction(finding.typology, finding.row_ids, finding.counterparty_id)
                    )
        for group in case.open_item_dispositions if case else []:
            if group.disposition == "close_benign":
                closures.extend(
                    Closure(item_id, items[item_id].ledger_row_id)
                    for item_id in group.item_ids
                    if item_id in items
                )
    return predictions, closures


def evaluate_agent(
    root: Path,
    client: ClaudeClient,
    *,
    max_clusters: int,
    seed: int,
    run_dir: Path,
    config: AgentConfig | None = None,
    skills: tuple[Skill, ...] | None = None,
) -> AgentEvaluation:
    store = LedgerStore(public_dir(root))
    queue = PendingQueue(run_dir / PENDING_QUEUE_FILENAME)
    tools = LedgerTools(store, queue)
    try:
        clusters = build_clusters(tools.findings, tools.open_items, store)
        sample = sample_clusters(clusters, max_clusters, seed)
        orchestrator = Orchestrator(client, tools, config=config, skills=skills)
        outcomes = orchestrator.run(sample)
        write_outcomes(outcomes, run_dir / "outcomes.jsonl")

        truth = load_truth(hidden_dir(root))
        scope = {row for cluster in sample for row in cluster.row_ids}
        predictions, closures = outcomes_to_predictions(outcomes, sample, tools)
        agent_card = score(truth, predictions, closures, scope_row_ids=scope)

        items = {item.item_id: item for item in tools.open_items}
        sample_finding_ids = {f for c in sample for f in c.finding_ids}
        sample_item_ids = {i for c in sample for i in c.open_item_ids}
        baseline_predictions = [
            Prediction(f.typology, f.row_ids, f.counterparty_id)
            for f in tools.findings
            if f.finding_id in sample_finding_ids
        ]
        sampled_match = MatchResult(open_items=[items[i] for i in sorted(sample_item_ids)])
        baseline_closed = [
            Closure(d.item_id, d.ledger_row_id) for d in baseline_closures(sampled_match)
        ]
        baseline_card = score(truth, baseline_predictions, baseline_closed, scope_row_ids=scope)
        return AgentEvaluation(agent_card, baseline_card, outcomes, len(clusters), len(sample))
    finally:
        store.close()
