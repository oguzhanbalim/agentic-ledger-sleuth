"""Orchestrator: clusters, then investigator, evidence gate and reviewer for each cluster.

The orchestrator is code. It decides what each role sees and what happens to its output, and a
model cannot route around it: a case file reaches the reviewer only after passing the gate, and
reaches the accepted pile only after both. Anything else stays with a person, and any draft behind
a failed case is withdrawn.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import anthropic

from ledger_sleuth.agent.clusters import Cluster
from ledger_sleuth.agent.loop import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    ClaudeClient,
    Conversation,
    LoopResult,
    OutputT,
    ToolCallRecord,
    Usage,
)
from ledger_sleuth.agent.prompts import investigator_system, reviewer_system
from ledger_sleuth.agent.schemas import CaseFile, ReviewVerdict
from ledger_sleuth.agent.skills import Skill, load_skills
from ledger_sleuth.evidence import EvidenceReport, check_case_file
from ledger_sleuth.tools import TOOL_SPECS, LedgerTools, ToolSpec

MAX_GATE_RETRIES = 2
MAX_REVIEW_ROUNDS = 2

READ_ONLY_SPECS = tuple(spec for spec in TOOL_SPECS if spec.read_only)

OutcomeStatus = Literal[
    "accepted",
    "rejected_by_gate",
    "escalated",
    "refused",
    "truncated",
    "failed",
]


@dataclass(slots=True)
class CaseOutcome:
    cluster_id: str
    status: OutcomeStatus
    case_file: CaseFile | None = None
    gate_errors: list[str] = field(default_factory=list)
    review: ReviewVerdict | None = None
    detail: str | None = None
    tool_log: list[ToolCallRecord] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "status": self.status,
            "case_file": self.case_file.model_dump(mode="json") if self.case_file else None,
            "gate_errors": self.gate_errors,
            "review": self.review.model_dump(mode="json") if self.review else None,
            "detail": self.detail,
            "tool_log": [asdict(call) for call in self.tool_log],
        }


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_turns: int = DEFAULT_MAX_TURNS


class Orchestrator:
    def __init__(
        self,
        client: ClaudeClient,
        tools: LedgerTools,
        *,
        config: AgentConfig | None = None,
        skills: tuple[Skill, ...] | None = None,
    ) -> None:
        self._client = client
        self._tools = tools
        self._config = config or AgentConfig()
        loaded = skills if skills is not None else load_skills()
        self._investigator_system = investigator_system(loaded)
        self._reviewer_system = reviewer_system(loaded)
        self.usage = Usage()

    def _investigator(self) -> Conversation[CaseFile]:
        return self._conversation(self._investigator_system, TOOL_SPECS, CaseFile)

    def _reviewer(self) -> Conversation[ReviewVerdict]:
        # The reviewer gets no write tool: it can judge a case, not act on it.
        return self._conversation(self._reviewer_system, READ_ONLY_SPECS, ReviewVerdict)

    def _conversation(
        self, system: str, specs: tuple[ToolSpec, ...], output_model: type[OutputT]
    ) -> Conversation[OutputT]:
        return Conversation(
            self._client,
            system=system,
            tools=self._tools,
            specs=specs,
            output_model=output_model,
            model=self._config.model,
            effort=self._config.effort,
            max_tokens=self._config.max_tokens,
            max_turns=self._config.max_turns,
            usage=self.usage,
        )

    def cluster_brief(self, cluster: Cluster) -> str:
        """The per-cluster part of the prompt: computed facts only, serialised deterministically."""
        findings = [self._tools.finding(f) for f in cluster.finding_ids]
        items = {item.item_id: item for item in self._tools.open_items}
        brief = {
            "cluster_id": cluster.cluster_id,
            "group": cluster.group_key,
            "exposure_try_minor": cluster.exposure_try_minor,
            "findings": [asdict(f) for f in findings if f is not None],
            "open_items": [asdict(items[i]) for i in cluster.open_item_ids],
        }
        return "Investigate this cluster and return its case file.\n\n" + json.dumps(
            brief, sort_keys=True, indent=1
        )

    def investigate(self, cluster: Cluster) -> CaseOutcome:
        investigator = self._investigator()
        outcome = self._run_case(cluster, investigator)
        outcome.tool_log = investigator.tool_calls
        if outcome.status != "accepted":
            self._withdraw_drafts(outcome)
        return outcome

    def _run_case(self, cluster: Cluster, investigator: Conversation[CaseFile]) -> CaseOutcome:
        result = investigator.send(self.cluster_brief(cluster))
        verdict: ReviewVerdict | None = None
        for _ in range(MAX_REVIEW_ROUNDS + 1):
            result, gate = self._through_gate(cluster, investigator, result)
            if result.status != "completed" or result.output is None:
                return _from_loop(cluster, result)
            case = result.output
            if not gate.ok:
                return CaseOutcome(
                    cluster.cluster_id, "rejected_by_gate", case, gate_errors=gate.errors
                )
            review = self._reviewer().send(self._review_brief(cluster, case))
            if review.status != "completed" or review.output is None:
                outcome = _from_loop(cluster, review)
                outcome.case_file = case
                return outcome
            verdict = review.output
            if verdict.verdict == "accept":
                return CaseOutcome(cluster.cluster_id, "accepted", case, review=verdict)
            if verdict.verdict == "escalate":
                return CaseOutcome(cluster.cluster_id, "escalated", case, review=verdict)
            result = investigator.send(
                "The reviewer sent the case file back:\n- "
                + "\n- ".join(verdict.objections)
                + "\nAddress these and return the corrected case file."
            )
        return CaseOutcome(
            cluster.cluster_id,
            "escalated",
            result.output,
            review=verdict,
            detail="review rounds exhausted",
        )

    def _through_gate(
        self,
        cluster: Cluster,
        investigator: Conversation[CaseFile],
        result: LoopResult[CaseFile],
    ) -> tuple[LoopResult[CaseFile], EvidenceReport]:
        """Re-prompt on gate failures, within a bound. The model cannot argue with the verdict."""
        report = EvidenceReport()
        for attempt in range(MAX_GATE_RETRIES + 1):
            if result.status != "completed" or result.output is None:
                return result, report
            report = check_case_file(
                result.output,
                self._tools,
                expected_finding_ids=cluster.finding_ids,
                expected_item_ids=cluster.open_item_ids,
            )
            if report.ok or attempt == MAX_GATE_RETRIES:
                return result, report
            result = investigator.send(
                "The evidence gate rejected this case file:\n- "
                + "\n- ".join(report.errors)
                + "\nFix these and return the full corrected case file."
            )
        return result, report

    def _review_brief(self, cluster: Cluster, case: CaseFile) -> str:
        return (
            f"{self.cluster_brief(cluster)}\n\nThe investigator's case file, which has passed "
            f"the evidence gate:\n\n{case.model_dump_json(indent=1)}"
        )

    def _withdraw_drafts(self, outcome: CaseOutcome) -> None:
        if outcome.case_file is None:
            return
        for draft_id in outcome.case_file.adjustment_draft_ids:
            if self._tools.queue.get(draft_id) is not None:
                self._tools.queue.withdraw(draft_id, f"case outcome: {outcome.status}")

    def run(
        self,
        clusters: Iterable[Cluster],
        *,
        on_outcome: Callable[[CaseOutcome], None] | None = None,
    ) -> list[CaseOutcome]:
        outcomes = []
        for cluster in clusters:
            try:
                outcome = self.investigate(cluster)
            except (
                anthropic.AuthenticationError,
                anthropic.PermissionDeniedError,
                anthropic.BadRequestError,
                anthropic.NotFoundError,
            ):
                raise  # Configuration errors repeat on every cluster: stop, do not burn through.
            except anthropic.APIError as exc:
                outcome = CaseOutcome(cluster.cluster_id, "failed", detail=str(exc))
            outcomes.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)
        return outcomes


def _from_loop(cluster: Cluster, result: LoopResult[Any]) -> CaseOutcome:
    status: OutcomeStatus = {
        "refused": "refused",
        "truncated": "truncated",
    }.get(result.status, "failed")  # type: ignore[assignment]
    return CaseOutcome(cluster.cluster_id, status, detail=result.detail or result.status)


def write_outcomes(outcomes: Iterable[CaseOutcome], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for outcome in outcomes:
            handle.write(json.dumps(outcome.to_record(), sort_keys=True) + "\n")
