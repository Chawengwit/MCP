"""OAuth-aware Bearer middleware for the HTTP transport (Phase 9).

This wraps the Phase 8 ``bearer_auth_middleware``: when a request
carries a Bearer token, we try to resolve it through the OAuth Provider
store first, then fall back to the static-token comparison if that
fails. Either way, the resolved identity (or ``None`` for the static
path) is attached to ``scope["state"]["mcp_user"]`` so downstream tools
can read it without re-parsing headers.

Key invariants (mirroring :mod:`src.transport.http`):

- **Pure ASGI** — `async def middleware(scope, receive, send)`, never
  ``BaseHTTPMiddleware`` (which buffers and would break SSE streams from
  the MCP SDK).
- **stderr-only logging.** No log line ever interpolates a token value,
  even at DEBUG.
- ``WWW-Authenticate`` on 401 carries
  ``resource_metadata="<issuer>/.well-known/oauth-protected-resource"``
  per the MCP Authorization spec (RFC 9728 §5.1).
- Some paths are public (discovery / register / authorize / token);
  the middleware lets them through without inspecting the header.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from .store import OAuthStore

BEARER_PREFIX = b"Bearer "
SCOPE_STATE_KEY = "mcp_user"

# Public endpoints that do NOT require a Bearer token. The OAuth dispatcher
# answers some of these; we just need to make sure the middleware does not
# 401 the unauthenticated request before the dispatcher gets a chance.
#
# Matching is exact-path-or-slash-boundary (see :func:`_is_public_path`):
# ``/register`` matches itself and ``/register/...`` but NOT ``/registerabc``.
# This prevents a future route accidentally inheriting the bypass.
PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/.well-known",
    "/register",
    "/authorize",
    "/token",
)


def oauth_aware_bearer_middleware(
    app: ASGIApp,
    *,
    store: OAuthStore,
    static_token: str,
    issuer: str,
) -> ASGIApp:
    """Construct the wrapped middleware. See module docstring."""
    static_token_bytes = static_token.encode("utf-8") if static_token else b""
    resource_metadata_url = f"{issuer.rstrip('/')}/.well-known/oauth-protected-resource"

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        # Auth applies only to HTTP requests. lifespan, websocket, etc.
        # delegate directly to the inner app so the MCP session manager
        # can run its async-context lifecycle.
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if _is_public_path(path):
            # Ensure downstream code that reads scope["state"] never crashes.
            _ensure_state(scope)
            await app(scope, receive, send)
            return

        auth_header = _read_authorization_header(scope.get("headers", []))
        if not auth_header.startswith(BEARER_PREFIX):
            await _send_unauthorized(send, resource_metadata_url)
            return

        provided = auth_header[len(BEARER_PREFIX) :].strip()

        # 1) Try the OAuth Provider table.
        # Strict ascii — a malformed token (non-ascii bytes) cannot match a
        # token we issued, so fail fast rather than substituting replacement
        # chars and falling through to a guaranteed-miss DB lookup.
        try:
            token_str = provided.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            await _send_unauthorized(send, resource_metadata_url)
            return
        oauth_record = await store.get_access_token(token_str)
        if oauth_record is not None:
            _ensure_state(scope)[SCOPE_STATE_KEY] = {
                "user_id": oauth_record.user_id,
                "auth_source": "oauth",
                "client_id": oauth_record.client_id,
            }
            await app(scope, receive, send)
            return

        # 2) Fall back to static Bearer (Phase 8 back-compat).
        if static_token_bytes and len(provided) == len(static_token_bytes):
            if secrets.compare_digest(provided, static_token_bytes):
                _ensure_state(scope)[SCOPE_STATE_KEY] = {
                    "user_id": None,
                    "auth_source": "static_bearer",
                    "client_id": None,
                }
                await app(scope, receive, send)
                return

        # 3) No match anywhere — 401.
        await _send_unauthorized(send, resource_metadata_url)

    return middleware


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _is_public_path(path: str) -> bool:
    """True iff `path` is exactly one of the public endpoints OR a child.

    Slash-boundary match prevents leaking the bypass to unrelated routes
    that happen to share a prefix (e.g. ``/tokenizer`` must NOT match
    ``/token``).
    """
    for prefix in PUBLIC_PATH_PREFIXES:
        stripped = prefix.rstrip("/")
        if path == stripped or path.startswith(stripped + "/"):
            return True
    return False


def _read_authorization_header(headers: Iterable[tuple[bytes, bytes]]) -> bytes:
    for name, value in headers:
        if name == b"authorization":
            return value
    return b""


def _ensure_state(scope: Scope) -> dict[str, Any]:
    state_obj = scope.get("state")
    if not isinstance(state_obj, dict):
        state_obj = {}
        scope["state"] = state_obj
    return state_obj


async def _send_unauthorized(send: Send, resource_metadata_url: str) -> None:
    body = b'{"error":"invalid_token","error_description":"Bearer token required."}'
    www_auth = (
        f'Bearer resource_metadata="{resource_metadata_url}", error="invalid_token"'
    ).encode("ascii")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", www_auth),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
