"""One-shot probe to enumerate the live Dynatrace MCP tool surface.

Discovers tool names + input schemas so the recorder + DynatraceClient
parsing stay aligned with the upstream MCP. Not part of the test suite;
human-driven discovery only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def _drive() -> int:
    from dotenv import load_dotenv
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    if not os.environ.get("DT_ENVIRONMENT"):
        print("FAIL: DT_ENVIRONMENT not set", file=sys.stderr)
        return 2

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@dynatrace-oss/dynatrace-mcp-server@latest"],
        env={**os.environ},
    )

    async with (
        stdio_client(server_params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        print(f"\n=== Discovered {len(tools.tools)} tools ===\n")
        for tool in tools.tools:
            print(f"## {tool.name}")
            if tool.description:
                print(f"   {tool.description[:120]}")
            if tool.inputSchema:
                props = tool.inputSchema.get("properties", {})
                required = tool.inputSchema.get("required", [])
                print(f"   args: {list(props.keys())}  required={required}")
            print()

        # Save to disk for downstream consumption.
        out = Path(__file__).resolve().parent / "_mcp_shape_snapshot.json"
        out.write_text(
            json.dumps(
                [
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.inputSchema,
                    }
                    for t in tools.tools
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nFull schema snapshot written to {out}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_drive()))
