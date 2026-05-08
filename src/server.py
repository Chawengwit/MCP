from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

# Project root = parent of src/. Ensure imports and relative paths work
# regardless of how the server is launched (e.g. by Claude Desktop from /).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from src.auth import Credentials, OAuth, OAuthConfig
from src.config import ApiConfig, load_api_configs
from src.events import Recorder
from src.gateway import GraphQLClient, RestClient
from src.tools import (
    FetchDataInput,
    GetStatusInput,
    GraphQLInput,
    SendDataInput,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    execute_graphql_handler,
    fetch_data_handler,
    get_status_handler,
    list_apis,
    send_data_handler,
)


def _build_oauth_configs(api_configs: dict[str, ApiConfig]) -> dict[str, OAuthConfig]:
    """Project the subset of api_configs that have auth.type == 'oauth2' into OAuthConfig.

    Required fields: client_id, client_secret, authorize_url, token_url. Missing or
    empty fields skip that api with a stderr warning — better than silently
    constructing an OAuth config that will fail at refresh time with a confusing
    `invalid_client` error from the upstream provider.

    `client_secret` MUST be supplied via `${VAR}` substitution in `api_configs.json`;
    storing literal secrets in the file is a mistake the config schema discourages
    but does not enforce.
    """
    out: dict[str, OAuthConfig] = {}
    for api_id, cfg in api_configs.items():
        if cfg.auth is None or (cfg.auth.type or "").lower() != "oauth2":
            continue
        provider = cfg.auth.provider or api_id
        client_id = cfg.auth.client_id
        client_secret = cfg.auth.client_secret
        authorize_url = cfg.auth.authorize_url
        token_url = cfg.auth.token_url
        scopes = cfg.auth.scopes or []
        missing: list[str] = []
        if not client_id:
            missing.append("client_id")
        if not client_secret:
            missing.append("client_secret")
        if not authorize_url:
            missing.append("authorize_url")
        if not token_url:
            missing.append("token_url")
        if missing:
            _log_error(
                f"Skipping OAuth config for {api_id}: missing {', '.join(missing)}. "
                f"Set ${{VAR}} placeholders in api_configs.json + values in .env."
            )
            continue
        # Type narrowing for mypy — the missing-field guard above ensures non-None.
        assert client_id is not None
        assert client_secret is not None
        assert authorize_url is not None
        assert token_url is not None
        try:
            out[api_id] = OAuthConfig(
                provider=provider,
                client_id=client_id,
                client_secret=client_secret,
                authorize_url=authorize_url,
                token_url=token_url,
                scopes=scopes,
                redirect_uri=cfg.auth.redirect_uri,
            )
        except ValidationError as exc:
            _log_error(f"Skipping OAuth config for {api_id}: {exc}")
    return out


def _build_tool_context(*, api_configs: dict[str, ApiConfig], recorder: Recorder) -> ToolContext:
    """Construct the ToolContext shared by every Phase 5 tool handler."""
    oauth = OAuth()
    credentials = Credentials(
        oauth=oauth,
        oauth_configs=_build_oauth_configs(api_configs),
    )

    def rest_factory(cfg: ApiConfig) -> RestClient:
        timeout = cfg.limits.timeout_seconds if cfg.limits else 30
        max_retries = cfg.limits.max_retries if cfg.limits else 3
        return RestClient(base_url=cfg.base_url, timeout_seconds=timeout, max_retries=max_retries)

    def graphql_factory(cfg: ApiConfig) -> GraphQLClient:
        timeout = cfg.limits.timeout_seconds if cfg.limits else 30
        max_retries = cfg.limits.max_retries if cfg.limits else 3
        return GraphQLClient(url=cfg.base_url, timeout_seconds=timeout, max_retries=max_retries)

    return ToolContext(
        configs=api_configs,
        credentials=credentials,
        rest_client_factory=rest_factory,
        graphql_client_factory=graphql_factory,
        recorder=recorder,
    )


def _build_registry(context: ToolContext) -> ToolRegistry:
    """Create a fresh ToolRegistry seeded with built-in + Phase 5 tools.

    Each handler captures the ToolContext via closure so the registered handler
    has signature `(session_id, arguments) -> dict` matching the registry contract.
    """
    registry = ToolRegistry()

    async def list_apis_h(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        return await list_apis(session_id, context.recorder, context.configs)

    async def fetch_data_h(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        return await fetch_data_handler(arguments, context=context)

    async def send_data_h(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        return await send_data_handler(arguments, context=context)

    async def execute_graphql_h(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        return await execute_graphql_handler(arguments, context=context)

    async def get_status_h(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        return await get_status_handler(arguments, context=context)

    registry.register(
        ToolSpec(
            name="list_apis",
            description=(
                "List all configured API services with their types, URLs, and available endpoints."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=list_apis_h,
        )
    )
    registry.register(
        ToolSpec(
            name="fetch_data",
            description="GET data from a configured REST API. Triggers OAuth if needed.",
            input_schema=FetchDataInput.model_json_schema(),
            handler=fetch_data_h,
        )
    )
    registry.register(
        ToolSpec(
            name="send_data",
            description="POST/PUT data to a configured REST API. Triggers OAuth if needed.",
            input_schema=SendDataInput.model_json_schema(),
            handler=send_data_h,
        )
    )
    registry.register(
        ToolSpec(
            name="execute_graphql",
            description="Execute a GraphQL query/mutation against a configured GraphQL API.",
            input_schema=GraphQLInput.model_json_schema(),
            handler=execute_graphql_h,
        )
    )
    registry.register(
        ToolSpec(
            name="get_status",
            description=(
                "Report authentication state per configured API. Read-only — never opens a browser."
            ),
            input_schema=GetStatusInput.model_json_schema(),
            handler=get_status_h,
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

    # MCP_API_CONFIG_PATH overrides the default for test isolation and for
    # operators who keep config under XDG_CONFIG_HOME or similar.
    config_path = Path(os.getenv("MCP_API_CONFIG_PATH", "config/api_configs.json"))
    try:
        api_configs = load_api_configs(config_path)
    except Exception as e:
        _log_error(f"Failed to load API configuration: {e}")
        sys.exit(1)

    _log(f"Loaded {len(api_configs)} API configurations")

    recorder = Recorder.from_env()
    await recorder.start()
    _log("Activity logging started")

    context = _build_tool_context(api_configs=api_configs, recorder=recorder)
    registry = _build_registry(context)
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
