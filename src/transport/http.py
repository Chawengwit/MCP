"""HTTP (Streamable HTTP) transport for the MCP Data Gateway.

Exposes the MCP server as an ASGI app on `POST/GET/DELETE /mcp`, served by
uvicorn. Bearer-token auth is enforced by a pure-ASGI middleware (NOT
``BaseHTTPMiddleware`` — the latter buffers responses and breaks the SSE
stream the MCP SDK can return for long tool calls).

Composition order, outermost first::

    bearer_auth_middleware
        └─ dispatcher (custom ASGI callable)
              ├─ HTTP scope, path == "/mcp"  →  StreamableHTTPSessionManager
              ├─ HTTP scope, other path      →  404 {"error":"not_found"}
              └─ lifespan (and other types)  →  empty Starlette app
                                                ↑ runs `async with manager.run()`

Why a custom dispatcher instead of Starlette ``Mount``/``Route``: ``Mount``
adds a 307 redirect for trailing-slash mismatches (``/mcp`` → ``/mcp/``)
which is ugly for HTTP clients; ``Route`` wraps endpoints in a
Request/Response cycle that doesn't fit the SDK's pure-ASGI
``handle_request(scope, receive, send)`` signature.

The ``StreamableHTTPSessionManager`` lifecycle is owned by the empty
Starlette app's lifespan. Per the MCP SDK, ``manager.run()`` is one-shot —
a fresh manager (and therefore a fresh app) is built per ``build_app``
call; tests that exercise the HTTP path build their own.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MCP_PATH = "/mcp"
BEARER_PREFIX = b"Bearer "

# Phase 9.6 — local HTTPS support so Claude Desktop's Custom Connector UI
# (which rejects http:// URLs) can talk to a server running on the operator's
# machine. Operators generate certs with `mkcert 127.0.0.1` (so macOS trusts
# them without a browser warning) and point these env vars at the resulting
# files.
ENV_TLS_CERT = "MCP_HTTP_TLS_CERT"
ENV_TLS_KEY = "MCP_HTTP_TLS_KEY"

# 401 body — short, no token echo, no internal details.
_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'
_NOT_FOUND_BODY = b'{"error":"not_found"}'


class LoopbackGuardError(RuntimeError):
    """Raised at startup when MCP_HTTP_HOST is non-loopback and no Bearer token is set.

    Public binds without authentication are refused at the boundary, fail-loud,
    rather than silently launching an unauthenticated server.
    """


def check_loopback_guard(host: str, token: str, *, oauth_enabled: bool = False) -> None:
    """Refuse non-loopback bind without a bearer token.

    A loopback bind (`127.0.0.1`, `::1`, `localhost`) without a token is
    permitted — that's the dev/test path and the kernel already prevents
    remote access. Any other host without a token raises.

    Phase 9: when the OAuth Provider is enabled (``oauth_enabled=True``),
    per-user OAuth tokens replace the static-token requirement, so a
    public bind without ``MCP_HTTP_BEARER_TOKEN`` is acceptable.
    ``MCP_OAUTH_ENCRYPTION_KEY`` (checked by the caller via
    ``is_oauth_provider_enabled``) is the canonical "OAuth is on" signal.
    """
    if host in LOOPBACK_HOSTS or token or oauth_enabled:
        return
    raise LoopbackGuardError(
        f"Refusing to bind {host!r} without MCP_HTTP_BEARER_TOKEN. "
        "Set the bearer token, enable the OAuth Provider "
        "(set MCP_OAUTH_ENCRYPTION_KEY), or bind to 127.0.0.1."
    )


def bearer_auth_middleware(app: ASGIApp, expected_token: str) -> ASGIApp:
    """Pure-ASGI middleware: requires `Authorization: Bearer <expected_token>`.

    No-op (passes everything through) when `expected_token` is empty —
    that path is reachable only on loopback per `check_loopback_guard`.

    Token comparison uses :func:`secrets.compare_digest` on equal-length
    byte strings. A length mismatch short-circuits to a 401 (still in
    constant time relative to mismatched-but-equal-length tokens, which is
    the threat we care about).
    """
    expected_bytes = expected_token.encode("utf-8") if expected_token else b""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        # Pass through lifespan / websocket / other non-HTTP scopes; auth is
        # only meaningful for HTTP requests, and lifespan must reach the inner
        # app so manager.run() enters/exits with the server lifecycle.
        if scope["type"] != "http" or not expected_bytes:
            await app(scope, receive, send)
            return

        auth_header = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth_header = value
                break

        if not auth_header.startswith(BEARER_PREFIX):
            await _send_unauthorized(send)
            return

        provided = auth_header[len(BEARER_PREFIX) :].strip()
        if len(provided) != len(expected_bytes) or not secrets.compare_digest(
            provided, expected_bytes
        ):
            await _send_unauthorized(send)
            return

        await app(scope, receive, send)

    return middleware


async def _send_unauthorized(send: Send) -> None:
    await _send_simple(send, 401, _UNAUTHORIZED_BODY)


async def _send_not_found(send: Send) -> None:
    await _send_simple(send, 404, _NOT_FOUND_BODY)


async def _send_simple(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_app(
    server: Server,
    expected_token: str,
    *,
    json_response: bool = False,
    oauth_dispatcher: Callable[[Scope, Receive, Send], Awaitable[bool]] | None = None,
    oauth_middleware: Callable[[ASGIApp], ASGIApp] | None = None,
) -> ASGIApp:
    """Build the ASGI app: lifespan-aware path dispatcher + Bearer middleware.

    The Streamable HTTP manager exposes a pure-ASGI `handle_request(scope,
    receive, send)` callable that doesn't fit Starlette's `Route`/`Mount`
    abstractions cleanly (Mount adds a trailing-slash redirect; Route wraps
    endpoints in a Request/Response cycle). So we use a small ASGI dispatcher:
    lifespan messages go to a no-route Starlette app (which drives the
    `manager.run()` context), HTTP requests for `/mcp` go straight to the
    manager, and everything else gets a 404.

    A fresh `StreamableHTTPSessionManager` is constructed per call — the SDK
    documents `.run()` as one-shot per instance, so callers (and each test)
    must build their own.

    `json_response=True` forces the manager to return plain JSON for every
    response (no SSE streaming). This is the easier path for clients that
    don't speak SSE; tests use it to keep TestClient interactions simple.
    """
    if not expected_token and oauth_middleware is None:
        # Defense for third-party callers who import build_app directly (the
        # run_http loopback guard wouldn't run on that path).
        print(
            "[mcp.transport.http] WARNING: building app with no bearer token. "
            "This is intended for loopback-only use; serving on a non-loopback "
            "address without a token allows unauthenticated access.",
            file=sys.stderr,
        )

    manager = StreamableHTTPSessionManager(app=server, json_response=json_response)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    # Empty Starlette app — its only job is to run the lifespan context.
    lifespan_app = Starlette(routes=[], lifespan=lifespan)

    async def dispatcher(scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "http":
            # Phase 9: OAuth Provider dispatcher gets first crack at HTTP
            # requests (its paths — /authorize, /token, /register,
            # /.well-known/* — never reach the MCP manager).
            if oauth_dispatcher is not None:
                handled = await oauth_dispatcher(scope, receive, send)
                if handled:
                    return
            if scope.get("path") == MCP_PATH:
                await manager.handle_request(scope, receive, send)
                return
            await _send_not_found(send)
            return
        # Lifespan and any other scope types delegate to the Starlette app so
        # `manager.run()` enters/exits with the server lifecycle.
        await lifespan_app(scope, receive, send)

    # Compose: CORS (outermost) → auth → dispatcher.
    # CORS must run before auth so that OPTIONS preflight responses don't
    # demand a Bearer token (browsers don't send credentials on preflight).
    from .cors import cors_middleware, resolve_allowed_origins

    if oauth_middleware is not None:
        # Phase 9: the OAuth-aware middleware wraps the dispatcher and
        # replaces the static-bearer one. It already accepts both the
        # OAuth tokens and (if present) the static fallback token.
        inner: ASGIApp = oauth_middleware(dispatcher)
    else:
        inner = bearer_auth_middleware(dispatcher, expected_token)
    return cors_middleware(inner, allowed_origins=resolve_allowed_origins())


def resolve_http_settings() -> tuple[str, int, str]:
    """Read and validate `MCP_HTTP_HOST`, `MCP_HTTP_PORT`, `MCP_HTTP_BEARER_TOKEN`.

    Raises ``ValueError`` for malformed port values (non-integer, out of the
    1..65535 range). The loopback guard is applied separately by `run_http`
    so callers can compose the checks differently in tests.
    """
    host = os.getenv("MCP_HTTP_HOST", DEFAULT_HOST).strip()
    port_raw = os.getenv("MCP_HTTP_PORT", str(DEFAULT_PORT)).strip()
    token = os.getenv("MCP_HTTP_BEARER_TOKEN", "").strip()

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"MCP_HTTP_PORT must be an integer, got {port_raw!r}") from exc
    if not (1 <= port <= 65535):
        raise ValueError(
            f"MCP_HTTP_PORT must be in the range 1..65535, got {port}. "
            "Pick an unprivileged port (>= 1024) for non-root processes."
        )
    return host, port, token


def resolve_tls_settings() -> tuple[str | None, str | None]:
    """Read and validate ``MCP_HTTP_TLS_CERT`` + ``MCP_HTTP_TLS_KEY``.

    Returns ``(cert_path, key_path)`` when TLS is configured, or
    ``(None, None)`` when both env vars are empty (TLS disabled, server
    speaks plain HTTP).

    Fail-loud rules:

    - Both vars must be set or both unset. Half-configured TLS is a
      footgun — uvicorn would silently start without TLS and the
      operator's "Add Custom Connector" flow in Claude Desktop would
      fail with an opaque "couldn't connect" error.
    - Both files must exist on disk. A typo'd path is easier to catch
      here than to debug from uvicorn's traceback.
    """
    cert = os.getenv(ENV_TLS_CERT, "").strip()
    key = os.getenv(ENV_TLS_KEY, "").strip()
    if not cert and not key:
        return None, None
    if not cert or not key:
        raise ValueError(
            f"{ENV_TLS_CERT} and {ENV_TLS_KEY} must both be set or both unset. "
            f"Got cert={'set' if cert else 'empty'}, key={'set' if key else 'empty'}."
        )
    if not Path(cert).is_file():
        raise ValueError(f"{ENV_TLS_CERT} points to a non-existent file: {cert}")
    if not Path(key).is_file():
        raise ValueError(f"{ENV_TLS_KEY} points to a non-existent file: {key}")
    return cert, key


async def run_http(
    server: Server,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
    oauth_dispatcher: Callable[[Scope, Receive, Send], Awaitable[bool]] | None = None,
    oauth_middleware: Callable[[ASGIApp], ASGIApp] | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> None:
    """Serve `server` over Streamable HTTP on `host:port`.

    Each kwarg defaults to ``None`` and falls back independently to its env
    counterpart (`MCP_HTTP_HOST` / `MCP_HTTP_PORT` / `MCP_HTTP_BEARER_TOKEN`)
    via :func:`resolve_http_settings`. So:

      - ``run_http(server)``
        — all three from env
      - ``run_http(server, host=h, port=p, token=t)``
        — all three explicit, env not read at all
      - ``run_http(server, host=h)``
        — explicit host; port + token from env

    The env read happens at most once per call (lazy: only fired when at
    least one kwarg is missing). Empty string is a valid explicit token
    value (means "no auth", reachable only on loopback per the guard);
    only ``None`` triggers env fallback for that slot.

    ``check_loopback_guard`` runs on the final resolved host/token regardless
    of how each arrived — the safety guarantee is enforced at this entry
    point, not at the caller.

    Lets uvicorn own SIGINT/SIGTERM — does NOT call `loop.add_signal_handler`.
    Configures `log_config=None` and `access_log=False` so uvicorn's default
    handlers are silenced and the project's stderr logger remains the only
    log path. The startup banner is emitted by the caller (`_serve_http` in
    `src/server.py`) so all server-level events go through one log function.
    """
    if host is None or port is None or token is None:
        env_host, env_port, env_token = resolve_http_settings()
        host = env_host if host is None else host
        port = env_port if port is None else port
        token = env_token if token is None else token

    if tls_cert is None and tls_key is None:
        tls_cert, tls_key = resolve_tls_settings()

    oauth_enabled = oauth_dispatcher is not None or oauth_middleware is not None
    check_loopback_guard(host, token, oauth_enabled=oauth_enabled)

    app = build_app(
        server,
        token,
        oauth_dispatcher=oauth_dispatcher,
        oauth_middleware=oauth_middleware,
    )
    # uvicorn quietly ignores ``ssl_certfile=None``, but passing the kwargs
    # only when set keeps the config call readable and lets tests assert the
    # presence/absence of TLS without inspecting None-valued keys.
    config_kwargs: dict[str, Any] = {
        "app": app,
        "host": host,
        "port": port,
        "log_config": None,
        "access_log": False,
    }
    if tls_cert and tls_key:
        config_kwargs["ssl_certfile"] = tls_cert
        config_kwargs["ssl_keyfile"] = tls_key
    config = uvicorn.Config(**config_kwargs)
    await uvicorn.Server(config).serve()
