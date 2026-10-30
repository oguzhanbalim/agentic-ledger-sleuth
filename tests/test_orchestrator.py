from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from helpers import ScriptedClient, message, text, tool_use
from ledger_sleuth.agent.clusters import MAX_CLUSTER_SIZE, Cluster, build_clusters
from ledger_sleuth.agent.orchestrator import MAX_GATE_RETRIES, Orchestrator, write_outcomes
from ledger_sleuth.agent.skills import SKILL_NAMES, load_skills, parse_skill
from ledger_sleuth.tools import LedgerTools, call_tool


@pytest.fixture
def cluster(tools: LedgerTools) -> Cluster:
    clusters = build_clusters(tools.findings, tools.open_items, tools.store)
    return next(c for c in clusters if c.finding_ids and len(c.finding_ids) <= 3)


def _good_case(cluster: Cluster, tools: LedgerTools, disposition: str = "benign") -> dict[str, Any]:
    claims = []
    for finding_id in cluster.finding_ids:
        finding = tools.finding(finding_id)
        assert finding is not None
        claims.append(
            {
                "statement": "Checked against the vendor history.",
                "row_ids": list(finding.row_ids),
                "finding_ids": [finding_id],
                "amount": None,
                "date": None,
            }
        )
    items = {i.item_id: i for i in tools.open_items}
    for item_id in cluster.open_item_ids:
        item = items[item_id]
        claims.append(
            {
                "statement": "Bank and ledger agree.",
                "row_ids": [r for r in [item.ledger_row_id, *item.bank_row_ids] if r],
                "finding_ids": [],
                "amount": None,
                "date": None,
            }
        )
    return {
        "cluster_id": cluster.cluster_id,
        "disposition": disposition,
        "summary": "Reviewed.",
        "claims": claims,
        "finding_dispositions": [
            {
                "finding_ids": list(cluster.finding_ids),
                "disposition": disposition,
                "reason": "Evidence reviewed.",
            }
        ],
        "open_item_dispositions": (
            [
                {
                    "item_ids": list(cluster.open_item_ids),
                    "disposition": "close_benign",
                    "reason": "Agrees.",
                }
            ]
            if cluster.open_item_ids
            else []
        ),
        "adjustment_draft_ids": [],
        "recommended_action": "None.",
    }


def _unsupported(case: dict[str, Any]) -> dict[str, Any]:
    fabricated = dict(case)
    first = dict(case["claims"][0])
    first["statement"] = "They paid 987654.32 too much."
    fabricated["claims"] = [first, *case["claims"][1:]]
    return fabricated


ACCEPT = {"verdict": "accept", "objections": []}


def test_clusters_cover_every_member_once(tools: LedgerTools) -> None:
    clusters = build_clusters(tools.findings, tools.open_items, tools.store)
    finding_ids = [f for c in clusters for f in c.finding_ids]
    item_ids = [i for c in clusters for i in c.open_item_ids]
    assert sorted(finding_ids) == sorted(f.finding_id for f in tools.findings)
    assert sorted(item_ids) == sorted(i.item_id for i in tools.open_items)
    assert all(len(c.finding_ids) + len(c.open_item_ids) <= MAX_CLUSTER_SIZE for c in clusters)
    assert len({c.cluster_id for c in clusters}) == len(clusters)
    assert [c.exposure_try_minor for c in clusters] == sorted(
        (c.exposure_try_minor for c in clusters), reverse=True
    )


def test_clusters_are_deterministic(tools: LedgerTools) -> None:
    assert build_clusters(tools.findings, tools.open_items, tools.store) == build_clusters(
        tools.findings, tools.open_items, tools.store
    )


def test_counterparty_findings_land_in_that_counterpartys_cluster(tools: LedgerTools) -> None:
    clusters = build_clusters(tools.findings, tools.open_items, tools.store)
    home = {f: c for c in clusters for f in c.finding_ids}
    for finding in tools.findings[:300]:
        if finding.counterparty_id:
            assert home[finding.finding_id].counterparty_id == finding.counterparty_id


def test_accepted_path_runs_investigator_gate_reviewer(
    tools: LedgerTools, cluster: Cluster
) -> None:
    client = ScriptedClient(
        [
            message(
                [tool_use("get_rule_findings", {"finding_ids": list(cluster.finding_ids)})],
                stop_reason="tool_use",
            ),
            message([text(_good_case(cluster, tools))]),
            message([text(ACCEPT)]),
        ]
    )
    outcome = Orchestrator(client, tools).investigate(cluster)
    assert outcome.status == "accepted"
    assert [call.name for call in outcome.tool_log] == ["get_rule_findings"]
    assert outcome.case_file is not None
    requests = client.messages.requests
    assert len(requests) == 3
    assert "investigator" in requests[0]["system"][0]["text"]
    assert "reviewer" in requests[2]["system"][0]["text"]


def test_both_skills_reach_both_roles(tools: LedgerTools, cluster: Cluster) -> None:
    client = ScriptedClient([message([text(_good_case(cluster, tools))]), message([text(ACCEPT)])])
    Orchestrator(client, tools).investigate(cluster)
    for request in client.messages.requests:
        system = request["system"][0]["text"]
        for skill in load_skills():
            assert skill.body in system
    assert {s.name for s in load_skills()} == set(SKILL_NAMES)


def test_reviewer_is_offered_no_write_tool(tools: LedgerTools, cluster: Cluster) -> None:
    client = ScriptedClient([message([text(_good_case(cluster, tools))]), message([text(ACCEPT)])])
    Orchestrator(client, tools).investigate(cluster)
    investigator, reviewer = client.messages.requests
    assert "propose_adjustment" in {d["name"] for d in investigator["tools"]}
    assert "propose_adjustment" not in {d["name"] for d in reviewer["tools"]}


def test_gate_failure_is_fed_back_and_reviewer_waits(tools: LedgerTools, cluster: Cluster) -> None:
    case = _good_case(cluster, tools)
    client = ScriptedClient(
        [
            message([text(_unsupported(case))]),
            message([text(case)]),
            message([text(ACCEPT)]),
        ]
    )
    outcome = Orchestrator(client, tools).investigate(cluster)
    assert outcome.status == "accepted"
    feedback = client.messages.requests[1]["messages"][-1]
    assert feedback["role"] == "user"
    assert "evidence gate rejected" in feedback["content"]
    assert "987654.32" in feedback["content"]
    # Only the third request is the reviewer: nothing unsupported ever reached it.
    assert "reviewer" not in client.messages.requests[1]["system"][0]["text"]


def test_gate_is_final_after_retries_and_reviewer_never_runs(
    tools: LedgerTools, cluster: Cluster
) -> None:
    finding = tools.finding(cluster.finding_ids[0])
    assert finding is not None
    row = tools.store.ledger_by_id[finding.row_ids[0]]
    drafted = call_tool(
        tools,
        "propose_adjustment",
        {
            "target_row_id": row.row_id,
            "adjustment_type": "hold_payment",
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "reason": "Hold until reviewed.",
            "evidence_row_ids": [row.row_id],
        },
    )
    bad = _unsupported(_good_case(cluster, tools))
    bad["adjustment_draft_ids"] = [drafted["draft_id"]]
    client = ScriptedClient([message([text(bad)]) for _ in range(MAX_GATE_RETRIES + 1)])

    outcome = Orchestrator(client, tools).investigate(cluster)

    assert outcome.status == "rejected_by_gate"
    assert any("987654.32" in e for e in outcome.gate_errors)
    assert all("reviewer" not in r["system"][0]["text"] for r in client.messages.requests)
    assert client.messages.script == []
    draft = tools.queue.get(drafted["draft_id"])
    assert draft is not None
    assert draft.status == "withdrawn"


def _draft_call(tools: LedgerTools, cluster: Cluster) -> dict[str, Any]:
    finding = tools.finding(cluster.finding_ids[0])
    assert finding is not None
    row = tools.store.ledger_by_id[finding.row_ids[0]]
    return tool_use(
        "propose_adjustment",
        {
            "target_row_id": row.row_id,
            "adjustment_type": "hold_payment",
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "reason": "Hold until reviewed.",
            "evidence_row_ids": [row.row_id],
        },
    )


def test_draft_cited_by_an_accepted_case_stays_pending(
    tools: LedgerTools, cluster: Cluster
) -> None:
    call = _draft_call(tools, cluster)
    case = _good_case(cluster, tools)
    case["claims"][0]["row_ids"].append(call["input"]["target_row_id"])

    class Client:
        """Fills in the draft id the queue assigned, which a script cannot know in advance."""

        def __init__(self) -> None:
            self.messages = self
            self.turn = 0

        def create(self, **kwargs: Any) -> Any:
            self.turn += 1
            if self.turn == 1:
                return message([call], stop_reason="tool_use")
            if self.turn == 2:
                case["adjustment_draft_ids"] = [tools.queue.all_drafts()[0].draft_id]
                return message([text(case)])
            return message([text(ACCEPT)])

    assert Orchestrator(Client(), tools).investigate(cluster).status == "accepted"
    assert [d.status for d in tools.queue.all_drafts()] == ["pending_human_approval"]


def test_draft_left_out_of_an_accepted_case_is_withdrawn(
    tools: LedgerTools, cluster: Cluster
) -> None:
    client = ScriptedClient(
        [
            message([_draft_call(tools, cluster)], stop_reason="tool_use"),
            message([text(_good_case(cluster, tools))]),
            message([text(ACCEPT)]),
        ]
    )
    assert Orchestrator(client, tools).investigate(cluster).status == "accepted"
    assert [d.status for d in tools.queue.all_drafts()] == ["withdrawn"]


def test_draft_from_a_refused_run_is_withdrawn(tools: LedgerTools, cluster: Cluster) -> None:
    client = ScriptedClient(
        [
            message([_draft_call(tools, cluster)], stop_reason="tool_use"),
            message([], stop_reason="refusal", stop_details={"type": "refusal", "category": None}),
        ]
    )
    assert Orchestrator(client, tools).investigate(cluster).status == "refused"
    assert [d.status for d in tools.queue.all_drafts()] == ["withdrawn"]


def test_reviewer_send_back_returns_to_the_same_conversation(
    tools: LedgerTools, cluster: Cluster
) -> None:
    case = _good_case(cluster, tools)
    client = ScriptedClient(
        [
            message([text(case)]),
            message([text({"verdict": "send_back", "objections": ["Check the refund row."]})]),
            message([text(case)]),
            message([text(ACCEPT)]),
        ]
    )
    outcome = Orchestrator(client, tools).investigate(cluster)
    assert outcome.status == "accepted"
    second_pass = client.messages.requests[2]
    assert "Check the refund row." in second_pass["messages"][-1]["content"]
    assert len(second_pass["messages"]) == 3


def test_escalation_is_not_acceptance(tools: LedgerTools, cluster: Cluster) -> None:
    client = ScriptedClient(
        [
            message([text(_good_case(cluster, tools))]),
            message([text({"verdict": "escalate", "objections": ["Needs a call."]})]),
        ]
    )
    assert Orchestrator(client, tools).investigate(cluster).status == "escalated"


def test_endless_send_back_ends_in_escalation(tools: LedgerTools, cluster: Cluster) -> None:
    case = _good_case(cluster, tools)
    back = {"verdict": "send_back", "objections": ["Again."]}
    client = ScriptedClient(
        [m for _ in range(5) for m in (message([text(case)]), message([text(back)]))]
    )
    outcome = Orchestrator(client, tools).investigate(cluster)
    assert outcome.status == "escalated"
    assert outcome.detail == "review rounds exhausted"


def test_investigator_refusal_becomes_an_outcome(tools: LedgerTools, cluster: Cluster) -> None:
    client = ScriptedClient(
        [message([], stop_reason="refusal", stop_details={"type": "refusal", "category": "bio"})]
    )
    outcome = Orchestrator(client, tools).investigate(cluster)
    assert outcome.status == "refused"
    assert "bio" in (outcome.detail or "")


def test_reviewer_refusal_keeps_the_case_but_does_not_accept(
    tools: LedgerTools, cluster: Cluster
) -> None:
    client = ScriptedClient(
        [
            message([text(_good_case(cluster, tools))]),
            message([], stop_reason="refusal", stop_details={"type": "refusal", "category": None}),
        ]
    )
    outcome = Orchestrator(client, tools).investigate(cluster)
    assert outcome.status == "refused"
    assert outcome.case_file is not None


def test_transient_api_error_fails_one_cluster_and_run_continues(
    tools: LedgerTools, cluster: Cluster
) -> None:
    request = httpx2.Request("POST", "https://example.invalid/v1/messages")
    client = ScriptedClient(
        [
            anthropic.APIConnectionError(request=request),
            message([text(_good_case(cluster, tools))]),
            message([text(ACCEPT)]),
        ]
    )
    outcomes = Orchestrator(client, tools).run([cluster, cluster])
    assert [o.status for o in outcomes] == ["failed", "accepted"]


def test_auth_error_stops_the_run(tools: LedgerTools, cluster: Cluster) -> None:
    request = httpx2.Request("POST", "https://example.invalid/v1/messages")
    response = httpx2.Response(401, request=request)
    client = ScriptedClient(
        [anthropic.AuthenticationError("bad key", response=response, body=None)]
    )
    with pytest.raises(anthropic.AuthenticationError):
        Orchestrator(client, tools).run([cluster, cluster])


def test_system_and_tools_identical_across_clusters(tools: LedgerTools) -> None:
    clusters = [
        c
        for c in build_clusters(tools.findings, tools.open_items, tools.store)
        if c.finding_ids and len(c.finding_ids) <= 3
    ][:2]
    script = []
    for cluster in clusters:
        script += [message([text(_good_case(cluster, tools))]), message([text(ACCEPT)])]
    client = ScriptedClient(script)
    Orchestrator(client, tools).run(clusters)
    first, _, third, _ = client.messages.requests
    assert first["system"] == third["system"]
    assert first["tools"] == third["tools"]
    assert first["messages"][0]["content"] != third["messages"][0]["content"]


def test_cluster_brief_carries_computed_facts(tools: LedgerTools, cluster: Cluster) -> None:
    brief = Orchestrator(ScriptedClient([]), tools).cluster_brief(cluster)
    assert cluster.cluster_id in brief
    assert cluster.finding_ids[0] in brief
    assert str(cluster.exposure_try_minor) in brief


def test_outcomes_serialise(tools: LedgerTools, cluster: Cluster, tmp_path: Path) -> None:
    client = ScriptedClient([message([text(_good_case(cluster, tools))]), message([text(ACCEPT)])])
    outcomes = Orchestrator(client, tools).run([cluster])
    write_outcomes(outcomes, tmp_path / "out" / "outcomes.jsonl")
    assert (tmp_path / "out" / "outcomes.jsonl").read_text().count("\n") == 1


def test_skill_parser_rejects_bad_files() -> None:
    with pytest.raises(ValueError, match="frontmatter"):
        parse_skill("# no frontmatter")
    with pytest.raises(ValueError, match="name and description"):
        parse_skill("---\nname: x\n---\nbody")
    skill = parse_skill("---\nname: x\ndescription: y\n---\nbody text")
    assert (skill.name, skill.description, skill.body) == ("x", "y", "body text")
