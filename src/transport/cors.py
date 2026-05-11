"""CORS middleware for the HTTP transport.

Browser-based MCP clients (MCP Inspector, Claude.ai's Custom Connector) hit
our endpoints cross-origin. Without ``Access-Control-Allow-*`` headers the
browser refuses to read the responses, even when the server otherwise
answers correctly. This module supplies a small, pure-ASGI CORS layer that
runs as the outermost middleware on the HTTP transport.

Scope: CORS is only enabled on paths that browser clients actually call:

  - ``/.well-known/oauth-authorization-server``  (RFC 8414 discovery)
  - ``/.well-known/oauth-protected-resource``    (RFC 9728 PRM)
  - ``/register``                                (RFC 7591 DCR)
  - ``/token``                                   (token exchange)
  - ``/mcp``                                     (the protected resource)

Paths that the browser visits as full-page navigation (``/authorize``,
``/authorize/consent``) do NOT need CORS — the browser is not running
the response through a fetch / XHR.

Configuration:

  ``MCP_CORS_ALLOWED_ORIGINS`` — comma-separated list of allowed origins.
  Default: ``http://localhost:6274,http://127.0.0.1:6274`` (MCP Inspector).
  Special value ``*`` opens to all origins (convenient for dev, less safe
  for production — operators must opt in explicitly).

Design choices:

  - Pure-ASGI: ``async def middleware(scope, receive, send)``. Mirrors
    :mod:`src.oauth_provider.middleware` rather than Starlette's
    ``CORSMiddleware`` which subclasses ``BaseHTTPMiddleware`` and breaks
    the SSE streams the MCP SDK can return.
  - The middleware ECHOES the request ``Origin`` back in
    ``Access-Control-Allow-Origin`` rather than emitting ``*`` — this is
    required when the request includes credentials, and avoids exposing
    that ``*`` is in use.
  - ``Mcp-Session-Id`` and ``WWW-Authenticate`` are exposed via
    ``Access-Control-Expose-Headers`` so browser clients can read the
    MCP session header and the OAuth challenge header.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ENV_ALLOWED_ORIGINS = "MCP_CORS_ALLOWED_ORIGINS"

DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:6274",
    "http://127.0.0.1:6274",
)

# Paths that respond to cross-origin fetch / XHR. Slash-boundary match
# (see :func:`_path_needs_cors`) — ``/tokenizer`` will NOT match ``/token``.
CORS_PATH_PREFIXES: tuple[str, ...] = (
    "/.well-known",
    "/register",
    "/token",
    "/mcp",
)

# Headers the OAuth + MCP flows actually use. Listing them explicitly (vs.
# echoing ``Access-Control-Request-Headers``) makes the policy auditable.
_ALLOW_HEADERS = b"Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id"

# Response headers browser clients must read to drive the protocol forward.
_EXPOSE_HEADERS = b"Mcp-Session-Id, WWW-Authenticate"

_ALLOW_METHODS = b"GET, POST, OPTIONS, DELETE"

# 24 hours — preflight responses are cacheable, no need to ping every call.
_PREFLIGHT_MAX_AGE = b"86400"


def resolve_allowed_origins() -> tuple[str, ...]:
    """Read and parse :data:`ENV_ALLOWED_ORIGINS`.

    Empty / unset → defaults (Inspector loopback).
    ``"*"`` → wildcard mode (every origin allowed; the middleware still
    echoes the request origin in the response).
    Comma-separated values are stripped and de-duplicated.
    """
    raw = os.getenv(ENV_ALLOWED_ORIGINS, "").strip()
    if not raw:
        return DEFAULT_ALLOWED_ORIGINS
    if raw == "*":
        return ("*",)
    seen: list[str] = []
    for entry in raw.split(","):
        cleaned = entry.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen) if seen else DEFAULT_ALLOWED_ORIGINS


def _path_needs_cors(path: str) -> bool:
    for prefix in CORS_PATH_PREFIXES:
        stripped = prefix.rstrip("/")
        if path == stripped or path.startswith(stripped + "/"):
            return True
    return False


def _is_origin_allowed(origin: str, allowed: Iterable[str]) -> bool:
    if not origin:
        return False
    allowed_set = set(allowed)
    if "*" in allowed_set:
        return True
    return origin in allowed_set


def _read_header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes:
    lower = name.lower()
    for raw_name, raw_value in headers:
        if raw_name.lower() == lower:
            return raw_value
    return b""


def cors_middleware(app: ASGIApp, *, allowed_origins: Iterable[str]) -> ASGIApp:
    """Wrap ``app`` with CORS handling for the configured paths.

    Pass-through for non-HTTP scopes (``lifespan``, etc.) and HTTP paths
    that aren't in :data:`CORS_PATH_PREFIXES`. For matching paths:

    - ``OPTIONS`` → 204 preflight response with the negotiated headers.
      Never delegates to ``app`` — preflight is a CORS concept, the inner
      app has nothing useful to say.
    - Other methods → wrap ``send`` so we can inject
      ``Access-Control-Allow-Origin`` / ``Vary: Origin`` /
      ``Access-Control-Expose-Headers`` into the response start frame.
    """
    allowed_tuple = tuple(allowed_origins)

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if not _path_needs_cors(path):
            await app(scope, receive, send)
            return

        origin = _read_header(scope.get("headers", []), b"origin").decode(
            "latin-1", errors="replace"
        )
        allowed = _is_origin_allowed(origin, allowed_tuple)

        method = (scope.get("method") or "").upper()
        if method == "OPTIONS":
            await _send_preflight(send, origin if allowed else None)
            return

        if not allowed:
            # Request reaches the inner app unchanged. The browser will
            # ultimately block the response because no allow-origin header
            # is set — exactly the right behavior for an un-allowlisted
            # origin. We do NOT 403 here because non-browser clients
            # (curl, server-to-server) don't send Origin and should
            # continue working.
            await app(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                headers.append((b"vary", b"Origin"))
                headers.append((b"access-control-expose-headers", _EXPOSE_HEADERS))
                # Credential-mode requests need this when the server sets
                # `Access-Control-Allow-Origin` to a specific origin (not *).
                headers.append((b"access-control-allow-credentials", b"true"))
                message["headers"] = headers
            await send(message)

        await app(scope, receive, send_with_cors)

    return middleware


async def _send_preflight(send: Send, allowed_origin: str | None) -> None:
    """204 No Content with CORS headers (or no CORS headers if origin denied)."""
    headers: list[tuple[bytes, bytes]] = [
        (b"content-length", b"0"),
        (b"vary", b"Origin"),
    ]
    if allowed_origin:
        headers.extend(
            [
                (b"access-control-allow-origin", allowed_origin.encode("latin-1")),
                (b"access-control-allow-methods", _ALLOW_METHODS),
                (b"access-control-allow-headers", _ALLOW_HEADERS),
                (b"access-control-expose-headers", _EXPOSE_HEADERS),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-max-age", _PREFLIGHT_MAX_AGE),
            ]
        )
    await send({"type": "http.response.start", "status": 204, "headers": headers})
    await send({"type": "http.response.body", "body": b""})
