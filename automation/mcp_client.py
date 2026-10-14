"""Minimal synchronous wrapper around the `mcp` SDK for calling a single
tool on a remote streamable-HTTP MCP server and getting its JSON content
back.

Every scraper's `sync_jobs(app)` runs on its own dedicated background
thread (see automation/tasks.py's per-source worker threads), so opening a
fresh asyncio event loop per call is safe and simple here - there's no
shared loop across threads to coordinate.

Failures (unreachable server, a non-200/error JSON-RPC response, a server
that rejects non-Claude clients) are left to propagate rather than swallowed
here, the same way scrapers/jobright.py lets a Playwright error propagate -
automation/tasks.run_sync_async already catches and surfaces sync failures
through the existing sync-status error UI.
"""

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def _call_tool_async(server_url, tool_name, arguments):
    async with streamable_http_client(server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            texts = [block.text for block in result.content if getattr(block, "text", None)]
            return "\n".join(texts)


def call_tool_json(server_url, tool_name, arguments=None):
    """Calls `tool_name` on the MCP server at `server_url` and parses its
    text content as JSON. Raises on any failure."""
    raw = asyncio.run(_call_tool_async(server_url, tool_name, arguments or {}))
    return json.loads(raw)
