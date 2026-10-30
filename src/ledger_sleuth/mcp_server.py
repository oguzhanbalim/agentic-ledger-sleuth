"""Exposes the tool surface over MCP, using the official SDK.

Every registered function is a thin wrapper over ``call_tool``, so validation and behaviour are the
same whether a request arrives from this server or from the agent loop.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ledger_sleuth.store import LedgerStore
from ledger_sleuth.tools import (
    PENDING_QUEUE_FILENAME,
    TOOL_SPECS,
    LedgerTools,
    PendingQueue,
    ToolSpec,
    call_tool,
)


def _wrapper(tools: LedgerTools, spec: ToolSpec) -> Any:
    """Build a function whose signature mirrors the input model, for schema generation."""
    parameters = []
    annotations: dict[str, Any] = {"return": dict[str, Any]}
    for name, field in spec.input_model.model_fields.items():
        default = inspect.Parameter.empty if field.is_required() else field.default
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )
        annotations[name] = field.annotation

    def handler(**kwargs: Any) -> dict[str, Any]:
        return call_tool(tools, spec.name, kwargs)

    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    handler.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    handler.__annotations__ = annotations
    return handler


def build_server(tools: LedgerTools) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        "ledger-sleuth",
        instructions=(
            "Read-only access to a synthetic ledger, plus one tool that drafts an adjustment "
            "for human approval. No tool posts to or changes the ledger."
        ),
    )
    for spec in TOOL_SPECS:
        server.add_tool(
            _wrapper(tools, spec),
            name=spec.name,
            description=spec.description,
            annotations=ToolAnnotations(
                read_only_hint=spec.read_only,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
    return server


def serve(public_dir: Path, queue_dir: Path) -> None:
    store = LedgerStore(public_dir)
    tools = LedgerTools(store, PendingQueue(queue_dir / PENDING_QUEUE_FILENAME))
    build_server(tools).run("stdio")
