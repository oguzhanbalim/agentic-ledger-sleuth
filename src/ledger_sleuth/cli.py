"""Command line entry point: generate, eval-baseline, investigate, eval-agent, mcp."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from ledger_sleuth.agent.clusters import build_clusters
from ledger_sleuth.agent.loop import DEFAULT_EFFORT, DEFAULT_MODEL, ClaudeClient
from ledger_sleuth.agent.orchestrator import AgentConfig, Orchestrator, write_outcomes
from ledger_sleuth.evals.baseline import baseline_report, dataset_root, ensure_dataset
from ledger_sleuth.evals.metrics import render
from ledger_sleuth.generator import (
    HELDOUT_SEED,
    TRAIN_SEED,
    GeneratorConfig,
    generate,
    hidden_dir,
    public_dir,
)
from ledger_sleuth.store import LedgerStore
from ledger_sleuth.tools import PENDING_QUEUE_FILENAME, LedgerTools, PendingQueue


def _cmd_generate(args: argparse.Namespace) -> int:
    root = dataset_root(args.data_root, args.seed)
    truth = generate(GeneratorConfig(seed=args.seed), root)
    print(f"seed:           {truth['seed']}")
    print(f"ledger rows:    {truth['ledger_rows']}")
    print(f"bank rows:      {truth['bank_rows']}")
    print(f"counterparties: {truth['counterparties']}")
    print(f"incidents:      {len(truth['incidents'])}")
    print(f"benign items:   {len(truth['benign'])}")
    print(f"data digest:    {truth['data_digest']}")
    print(f"visible data:   {public_dir(root)}")
    print(f"ground truth:   {hidden_dir(root)}")
    return 0


def _cmd_eval_baseline(args: argparse.Namespace) -> int:
    print(baseline_report(args.data_root, args.seeds))
    return 0


def _anthropic_client() -> ClaudeClient | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. The live agent needs it; "
            "`eval-baseline` and the test suite do not.",
            file=sys.stderr,
        )
        return None
    import anthropic

    return anthropic.Anthropic()


def _agent_config(args: argparse.Namespace) -> AgentConfig:
    return AgentConfig(model=args.model, effort=args.effort)


def _cmd_investigate(args: argparse.Namespace) -> int:
    client = _anthropic_client()
    if client is None:
        return 2
    root = ensure_dataset(args.data_root, args.seed)
    run_dir = args.run_root / f"seed-{args.seed}"
    store = LedgerStore(public_dir(root))
    try:
        tools = LedgerTools(store, PendingQueue(run_dir / PENDING_QUEUE_FILENAME))
        clusters = build_clusters(tools.findings, tools.open_items, store)[: args.max_clusters]
        orchestrator = Orchestrator(client, tools, config=_agent_config(args))
        outcomes = orchestrator.run(
            clusters, on_outcome=lambda o: print(f"{o.cluster_id}  {o.status}  {o.detail or ''}")
        )
        write_outcomes(outcomes, run_dir / "outcomes.jsonl")
    finally:
        store.close()
    print(json.dumps(asdict(orchestrator.usage), indent=1))
    print(f"case files: {run_dir / 'outcomes.jsonl'}")
    print(f"draft queue: {tools.queue.path}")
    return 0


def _cmd_eval_agent(args: argparse.Namespace) -> int:
    from ledger_sleuth.evals.agent_eval import evaluate_agent

    client = _anthropic_client()
    if client is None:
        return 2
    root = ensure_dataset(args.data_root, args.seed)
    result = evaluate_agent(
        root,
        client,
        max_clusters=args.max_clusters,
        seed=args.seed,
        run_dir=args.run_root / f"seed-{args.seed}-eval",
        config=_agent_config(args),
    )
    print(f"clusters run: {result.clusters_run} of {result.clusters_available}")
    print(render(result.baseline_on_sample, "rules baseline on the same sample"))
    print()
    print(render(result.agent, "agent"))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from ledger_sleuth.mcp_server import serve

    root = ensure_dataset(args.data_root, args.seed)
    serve(public_dir(root), args.run_root / f"seed-{args.seed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger-sleuth", description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--run-root", type=Path, default=Path("runs"))
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="build one deterministic dataset")
    gen.add_argument("--seed", type=int, default=TRAIN_SEED)
    gen.set_defaults(func=_cmd_generate)

    base = sub.add_parser("eval-baseline", help="score the rules; offline")
    base.add_argument("--seeds", type=int, nargs="+", default=[TRAIN_SEED, HELDOUT_SEED])
    base.set_defaults(func=_cmd_eval_baseline)

    for name, func, help_text in (
        ("investigate", _cmd_investigate, "run the agent on the highest-exposure clusters"),
        ("eval-agent", _cmd_eval_agent, "score the agent on a random sample of clusters"),
    ):
        agent = sub.add_parser(name, help=help_text)
        agent.add_argument("--seed", type=int, default=TRAIN_SEED)
        agent.add_argument("--max-clusters", type=int, default=25)
        agent.add_argument("--model", default=DEFAULT_MODEL)
        agent.add_argument("--effort", default=DEFAULT_EFFORT)
        agent.set_defaults(func=func)

    mcp_cmd = sub.add_parser("mcp", help="serve the tools over MCP on stdio")
    mcp_cmd.add_argument("--seed", type=int, default=TRAIN_SEED)
    mcp_cmd.set_defaults(func=_cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
