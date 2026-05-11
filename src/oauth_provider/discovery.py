"""OAuth 2.0 discovery endpoints.

Two well-known documents are served:

- ``/.well-known/oauth-authorization-server`` (RFC 8414) — describes
  the AS itself: endpoint URLs, supported grant types / methods.
- ``/.well-known/oauth-protected-resource`` (RFC 9728) — describes the
  protected resource (``/mcp``) and points clients at the AS.

Both are static documents derived from the configured ``issuer``. The
issuer URL is read once at construction; tests can pass a literal
issuer instead of relying on env.

The MCP Authorization spec (draft, May 2026) requires:

- RFC 9728 ``oauth-protected-resource`` discovery (MUST)
- ``code_challenge_methods_supported: ["S256"]`` in the AS metadata
  (RFC 8414 §2; required for PKCE discoverability)
- The 401 WWW-Authenticate header carries ``resource_metadata="<url>"``
  pointing at the protected-resource document (handled in middleware).
"""

from __future__ import annotations

import json
import os
from typing import Any

from starlette.types import Receive, Scope, Send

ENV_ISSUER = "MCP_OAUTH_ISSUER"

WELL_KNOWN_AS_PATH = "/.well-known/oauth-authorization-server"
WELL_KNOWN_RESOURCE_PATH = "/.well-known/oauth-protected-resource"


class DiscoveryError(RuntimeError):
    """Raised when the issuer URL is missing or malformed."""


def resolve_issuer(*, explicit: str | None = None) -> str:
    """Read the issuer URL.

    Order: explicit arg, then ``MCP_OAUTH_ISSUER`` env. The result must
    not include query string or fragment per RFC 8414 §2.
    """
    issuer = explicit if explicit is not None else os.getenv(ENV_ISSUER, "")
    issuer = issuer.strip().rstrip("/")
    if not issuer:
        raise DiscoveryError(
            f"{ENV_ISSUER} is required for OAuth discovery. Set it to the canonical "
            "HTTPS URL of this server (or the ngrok URL for local testing)."
        )
    if "?" in issuer or "#" in issuer:
        raise DiscoveryError(
            f"{ENV_ISSUER} must not contain a query string or fragment; got {issuer!r}."
        )
    return issuer


def build_authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """Return the body of ``/.well-known/oauth-authorization-server``."""
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        # Public-client model: no client_secret, no offline_access scope to advertise.
        "scopes_supported": ["mcp"],
        "service_documentation": f"{issuer}/.well-known/oauth-protected-resource",
    }


def build_protected_resource_metadata(issuer: str) -> dict[str, Any]:
    """Return the body of ``/.well-known/oauth-protected-resource`` (RFC 9728)."""
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
        "resource_documentation": issuer,
    }


async def discovery_handler(
    *,
    scope: Scope,
    send: Send,
    issuer: str,
) -> None:
    """Dispatch one well-known request to the correct metadata document.

    The caller (the OAuth dispatcher) is responsible for matching the
    path; this handler trusts the path and only emits the response.
    """
    path = scope.get("path", "")
    if path == WELL_KNOWN_AS_PATH:
        body = build_authorization_server_metadata(issuer)
    elif path == WELL_KNOWN_RESOURCE_PATH:
        body = build_protected_resource_metadata(issuer)
    else:  # pragma: no cover — caller guards the path
        await _send_json(send, 404, {"error": "not_found"})
        return
    await _send_json(send, 200, body)


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                # RFC 8414 / 9728 metadata is cacheable; one hour is conservative.
                (b"cache-control", b"public, max-age=3600"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


# Used by the request handlers to read the body of a discovery request
# without dragging in Starlette's Request abstraction.
async def _drain_request(receive: Receive) -> None:
    """Consume the request body so the connection is not left half-open."""
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            return
        more = bool(message.get("more_body", False))
