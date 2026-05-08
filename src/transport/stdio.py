"""stdio transport for the MCP Data Gateway.

Extracted from `src.server.main()` so the same `Server` instance can be served
over either stdio (this module) or HTTP (`src.transport.http`). The runtime
contract is unchanged from the pre-Phase 8 behavior.
"""

from __future__ import annotations

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server


async def run_stdio(server: Server, shutdown_event: asyncio.Event) -> None:
    """Drive `server` over stdio until the server task completes or `shutdown_event` fires.

    The caller installs SIGINT/SIGTERM handlers that flip `shutdown_event` — this
    function does NOT install signal handlers itself, so it composes cleanly with
    transports that own their own signal handling (e.g. uvicorn in HTTP mode).
    """
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        server_task = asyncio.create_task(
            server.run(read_stream, write_stream, init_options),
            name="mcp.transport.stdio.server",
        )
        shutdown_task = asyncio.create_task(
            shutdown_event.wait(), name="mcp.transport.stdio.shutdown"
        )

        done, pending = await asyncio.wait(
            {server_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
