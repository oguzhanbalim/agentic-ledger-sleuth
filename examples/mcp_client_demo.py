"""Starts the MCP server as a subprocess and talks to it with the official MCP client."""

import asyncio
import sys

from mcp import Client, StdioServerParameters


async def main() -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "ledger_sleuth.cli", "mcp", "--seed", "1729"]
    )
    async with Client(server) as client:
        listed = await client.list_tools()
        print("tools:", ", ".join(sorted(tool.name for tool in listed.tools)))
        writers = [tool.name for tool in listed.tools if not tool.annotations.read_only_hint]
        print("not read-only:", ", ".join(writers))
        result = await client.call_tool("run_matcher", {})
        print(result.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
