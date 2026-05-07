from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from src.config import ApiConfig, load_api_configs
from src.events import Recorder
from src.tools import ToolRegistry, ToolSpec, list_apis


def _build_registry(recorder: Recorder, api_configs: dict[str, ApiConfig]) -> ToolRegistry:
    """Create a fresh ToolRegistry seeded with built-in tools.

    Dependencies (recorder, api_configs) are bound via closures at registration time
    so each handler can be invoked with just (session_id, arguments).
    """
    registry = ToolRegistry()

    async def list_apis_handler(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        return await list_apis(session_id, recorder, api_configs)

    registry.register(
        ToolSpec(
            name="list_apis",
            description=(
                "List all configured API services with their types, URLs, and available endpoints."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=list_apis_handler,
        )
    )
    return registry


def _build_server(registry: ToolRegistry) -> Server:
    """Create an MCP Server with handlers wired to the registry."""
    server: Server = Server("mcp-data-gateway")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for spec in registry.all().values()
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        session_id = uuid4()
        spec = registry.get(name)

        if spec is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Tool '{name}' not found")],
                isError=True,
            )

        try:
            result = await spec.handler(session_id, arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                isError="error" in result,
            )
        except Exception as e:
            _log_error(f"Tool {name} raised exception: {e}")
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Tool execution failed: {str(e)}")],
                isError=True,
            )

    return server


async def main() -> None:
    """Main entry point for the MCP server."""
    _log("MCP Data Gateway starting...")

    config_path = Path("config/api_configs.json")
    try:
        api_configs = load_api_configs(config_path)
    except Exception as e:
        _log_error(f"Failed to load API configuration: {e}")
        sys.exit(1)

    _log(f"Loaded {len(api_configs)} API configurations")

    recorder = Recorder.from_env()
    await recorder.start()
    _log("Activity logging started")

    registry = _build_registry(recorder, api_configs)
    _log(f"Registered {len(registry.all())} tools")

    server = _build_server(registry)
    _log("MCP server initialized with tools")

    # Set up signal handlers — flip the event, let main() exit naturally.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signum: int) -> None:
        _log(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _request_shutdown, signal.SIGINT)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown, signal.SIGTERM)
        _log("Signal handlers registered")
    except NotImplementedError:
        # Windows does not support add_signal_handler.
        _log("Warning: Signal handlers not supported on this platform (Windows?)")

    _log("MCP server started, listening for messages on stdio...")

    try:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            server_task = asyncio.create_task(server.run(read_stream, write_stream, init_options))
            shutdown_task = asyncio.create_task(shutdown_event.wait())

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
    finally:
        await recorder.stop()
        _log("MCP server stopped")


def _log(msg: str) -> None:
    """Log message to stderr with timestamp."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", file=sys.stderr)


def _log_error(msg: str) -> None:
    """Log error message to stderr."""
    print(f"[ERROR] {msg}", file=sys.stderr)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        _log_error(f"Unhandled exception: {e}")
        sys.exit(1)
