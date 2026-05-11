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

# Load .env so ${VAR} placeholders in api_configs.json resolve.
from dotenv import load_dotenv  # noqa: E402

_dotenv_path = _PROJECT_ROOT / ".env"
if not load_dotenv(_dotenv_path):
    print(
        f"[mcp.server] No .env file found at {_dotenv_path}; "
        "relying on shell environment for ${VAR} substitution.",
        file=sys.stderr,
    )

from mcp import types  # noqa: E402
from mcp.server import Server  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from src.auth import Credentials, OAuth, OAuthConfig  # noqa: E402
from src.auth.service_api import authenticate as service_api_authenticate  # noqa: E402
from src.auth.session_login_keyring import KeyringServiceSessionStore  # noqa: E402
from src.config import ApiConfig, load_api_configs  # noqa: E402
from src.events import Recorder  # noqa: E402
from src.gateway import GraphQLClient, RestClient  # noqa: E402
from src.oauth_provider import (  # noqa: E402
    Encryptor,
    OAuthStore,
    ServiceSessionStore,
    is_oauth_provider_enabled,
)
from src.oauth_provider.discovery import resolve_issuer  # noqa: E402
from src.oauth_provider.middleware import oauth_aware_bearer_middleware  # noqa: E402
from src.oauth_provider.routes import build_oauth_dispatcher  # noqa: E402
from src.tools import (  # noqa: E402
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
from src.transport import (  # noqa: E402
    LoopbackGuardError,
    resolve_http_settings,
    run_http,
    run_stdio,
)

VALID_TRANSPORTS = ("stdio", "http")


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

    # Phase 9.4 — keyring-backed session_login store is wired in
    # unconditionally so both STDIO and HTTP transports can resolve a
    # ``session_login`` API from operator-managed credentials. In HTTP+OAuth
    # mode the OAuth Provider's store takes precedence; this is only the
    # fallback. In STDIO mode this is the only path (no OAuth flow exists).
    keyring_session_store = KeyringServiceSessionStore.from_configs(api_configs)

    return ToolContext(
        configs=api_configs,
        credentials=credentials,
        rest_client_factory=rest_factory,
        graphql_client_factory=graphql_factory,
        recorder=recorder,
        keyring_session_store=keyring_session_store,
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

    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in VALID_TRANSPORTS:
        _log_error(f"Unknown MCP_TRANSPORT={transport!r}; expected one of {VALID_TRANSPORTS}")
        await recorder.stop()
        sys.exit(1)

    try:
        if transport == "stdio":
            await _serve_stdio(server)
        else:  # transport == "http"
            await _serve_http(server, api_configs, context)
    finally:
        await recorder.stop()
        _log("MCP server stopped")


async def _serve_stdio(server: Server) -> None:
    """Run the server over stdio with SIGINT/SIGTERM handling."""
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
    await run_stdio(server, shutdown_event)


async def _serve_http(
    server: Server, api_configs: dict[str, ApiConfig], context: ToolContext
) -> None:
    """Run the server over Streamable HTTP. uvicorn owns SIGINT/SIGTERM.

    Resolves settings here (single env read) so the banner can include the
    bound address before `run_http` enters the uvicorn serve loop. Both the
    port-validation `ValueError` and the public-bind `LoopbackGuardError`
    exit through the same fail-loud path.

    When ``MCP_OAUTH_ENCRYPTION_KEY`` is set the OAuth Provider surface
    is composed on top of the MCP dispatcher — discovery / register /
    authorize / token become routable and the Bearer middleware accepts
    both per-user OAuth tokens and the legacy static token.
    """
    try:
        host, port, token = resolve_http_settings()
        oauth_components = await _maybe_build_oauth_components(api_configs, context)
        oauth_enabled = oauth_components is not None
        bearer_label = "set"
        if not token:
            bearer_label = "OAuth Provider" if oauth_enabled else "NOT set — loopback only"
        _log(f"HTTP transport listening on http://{host}:{port}/mcp (bearer={bearer_label})")
        if oauth_components is not None:
            dispatcher, middleware = oauth_components
            await run_http(
                server,
                host=host,
                port=port,
                token=token,
                oauth_dispatcher=dispatcher,
                oauth_middleware=middleware,
            )
        else:
            await run_http(server, host=host, port=port, token=token)
    except (ValueError, LoopbackGuardError) as exc:
        _log_error(str(exc))
        sys.exit(1)


async def _maybe_build_oauth_components(
    api_configs: dict[str, ApiConfig],
    context: ToolContext,
) -> tuple[Any, Any] | None:
    """Build the OAuth dispatcher + middleware pair, or return None.

    Returns ``None`` when ``MCP_OAUTH_ENCRYPTION_KEY`` is unset (i.e. the
    OAuth Provider is disabled). Failures while building the components
    raise — the operator wants a fail-loud signal that their config is
    broken rather than a silent fallback to static-bearer.
    """
    if not is_oauth_provider_enabled():
        return None

    # Pick the first session_login API as the "primary Service API" for the
    # consent flow. Phase 9 supports one Service API per server; multi-API
    # support is future work and would require extending the consent form
    # to pick which API to log in to.
    #
    # If no session_login API is configured we still mount the discovery /
    # register / token endpoints — consent will fail with a clear error
    # when attempted, but discovery probing keeps working (useful for
    # smoke tests, MCP Inspector exploration, and incremental setup).
    primary_api: ApiConfig | None = None
    for cfg in api_configs.values():
        if cfg.auth is not None and (cfg.auth.type or "").lower() == "session_login":
            primary_api = cfg
            break
    if primary_api is None:
        _log(
            "Warning: OAuth Provider is enabled but no API has auth.type=session_login. "
            "Discovery / register / token endpoints will mount, but the consent flow "
            "will fail until a Service API is configured."
        )
        # Construct a placeholder ApiConfig — the consent handler is only
        # ever invoked if a user POSTs to /authorize/consent, which they
        # cannot do without a registered client; the placeholder keeps
        # the rest of the surface working without dragging Optional[ApiConfig]
        # through the dispatcher signature.
        from src.config import ApiAuthConfig

        primary_api = ApiConfig(
            type="rest",
            base_url="http://127.0.0.1",
            auth=ApiAuthConfig(type="session_login", login_path="/_unconfigured"),
        )

    issuer = resolve_issuer()
    encryptor = Encryptor.from_env()
    store = OAuthStore.from_env()
    await store.init_db()
    service_store = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=primary_api,
        authenticate=service_api_authenticate,
    )

    # Phase 9.2 — wire the per-request session resolver into the ToolContext
    # so ``ensure_service_session`` can fetch the right user's session at
    # tool-call time. The middleware publishes the user_id to a contextvar;
    # tools then ask the store for that user's SessionInfo.
    context.service_session_store = service_store

    dispatcher = build_oauth_dispatcher(
        store=store,
        service_session_store=service_store,
        authenticate=service_api_authenticate,
        api_config=primary_api,
        issuer=issuer,
    )

    static_token = os.getenv("MCP_HTTP_BEARER_TOKEN", "").strip()

    def middleware_factory(app: Any) -> Any:
        return oauth_aware_bearer_middleware(
            app, store=store, static_token=static_token, issuer=issuer
        )

    return dispatcher, middleware_factory


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
