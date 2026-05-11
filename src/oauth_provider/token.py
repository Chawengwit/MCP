"""POST /token — OAuth 2.0 token endpoint.

Supports two grants:

- ``grant_type=authorization_code``: verifies PKCE, exchanges a
  single-use authorization code for an opaque access token (+ optional
  refresh token).
- ``grant_type=refresh_token``: rotates an existing refresh token into a
  new access token. The old row is deleted in the same transaction the
  new one is created (no overlap window).

Request bodies are ``application/x-www-form-urlencoded`` per RFC 6749
§3.2; ``application/json`` is also accepted as a courtesy to clients
that get the content-type wrong. JSON-only clients are a known
non-compliance pattern; rejecting them on a technicality is unfriendly
and adds no security.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qsl

from starlette.types import Receive, Scope, Send

from .pkce import verify_code_challenge
from .store import OAuthStore

# Access tokens live for one hour by default. Operators can tune via the
# ``MCP_OAUTH_ACCESS_TOKEN_TTL`` env var (positive integer seconds).
# Refresh tokens rotate per request and never carry an expires_at — they
# are valid until used or the client row is deleted.
_DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 3600
_ENV_ACCESS_TOKEN_TTL = "MCP_OAUTH_ACCESS_TOKEN_TTL"


def _resolve_access_token_ttl() -> int:
    """Read ``MCP_OAUTH_ACCESS_TOKEN_TTL`` once at import.

    Falls back to the default on malformed / non-positive values, with a
    stderr warning so misconfiguration is visible without crashing boot.
    """
    raw = os.getenv(_ENV_ACCESS_TOKEN_TTL, "").strip()
    if not raw:
        return _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        # Defer the warning to module-level print so it lands on stderr at
        # import time (logger config may not be ready yet during boot).
        import sys

        print(
            f"[mcp.oauth_provider.token] {_ENV_ACCESS_TOKEN_TTL}={raw!r} "
            f"is not an integer; using default {_DEFAULT_ACCESS_TOKEN_TTL_SECONDS}.",
            file=sys.stderr,
        )
        return _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    if parsed <= 0:
        import sys

        print(
            f"[mcp.oauth_provider.token] {_ENV_ACCESS_TOKEN_TTL}={parsed} must be > 0; "
            f"using default {_DEFAULT_ACCESS_TOKEN_TTL_SECONDS}.",
            file=sys.stderr,
        )
        return _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    return parsed


ACCESS_TOKEN_TTL_SECONDS = _resolve_access_token_ttl()


async def token_handler(
    *,
    scope: Scope,
    receive: Receive,
    send: Send,
    store: OAuthStore,
) -> None:
    if scope.get("method", "") != "POST":
        await _send_error(send, 405, "method_not_allowed", "Use POST.")
        return

    body_bytes = await _read_body(receive)
    form = _parse_request_body(scope, body_bytes)

    grant_type = form.get("grant_type", "")
    if grant_type == "authorization_code":
        await _handle_authorization_code(form=form, store=store, send=send)
        return
    if grant_type == "refresh_token":
        await _handle_refresh_token(form=form, store=store, send=send)
        return

    await _send_error(
        send,
        400,
        "unsupported_grant_type",
        "Only authorization_code and refresh_token are supported.",
    )


# ----------------------------------------------------------------------
# Grant handlers
# ----------------------------------------------------------------------


async def _handle_authorization_code(
    *,
    form: dict[str, str],
    store: OAuthStore,
    send: Send,
) -> None:
    required = ("code", "redirect_uri", "client_id", "code_verifier")
    for key in required:
        if not form.get(key):
            await _send_error(send, 400, "invalid_request", f"Missing required parameter: {key}")
            return

    code = form["code"]
    redirect_uri = form["redirect_uri"]
    client_id = form["client_id"]
    code_verifier = form["code_verifier"]

    consumed = await store.consume_authorization_code(code)
    if consumed is None:
        # consume_authorization_code returns None for both "unknown" and
        # "expired" — treat both as ``invalid_grant`` so attackers can't
        # tell expired-but-real from never-existed (no oracle).
        await _send_error(send, 400, "invalid_grant", "Authorization code is invalid or expired.")
        return

    if consumed.client_id != client_id:
        await _send_error(
            send, 400, "invalid_grant", "Authorization code was issued to a different client."
        )
        return

    if consumed.redirect_uri != redirect_uri:
        await _send_error(
            send, 400, "invalid_grant", "redirect_uri does not match the authorization request."
        )
        return

    if not verify_code_challenge(
        verifier=code_verifier,
        challenge=consumed.code_challenge,
        method=consumed.code_challenge_method,
    ):
        await _send_error(send, 400, "invalid_grant", "PKCE verification failed.")
        return

    token = await store.save_access_token(
        client_id=consumed.client_id,
        user_id=consumed.user_id,
        ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
    )
    await _send_token_response(send, token.token, token.refresh_token)


async def _handle_refresh_token(
    *,
    form: dict[str, str],
    store: OAuthStore,
    send: Send,
) -> None:
    refresh = form.get("refresh_token", "")
    if not refresh:
        await _send_error(send, 400, "invalid_request", "Missing required parameter: refresh_token")
        return

    client_id = form.get("client_id", "")
    if not client_id:
        await _send_error(send, 400, "invalid_request", "Missing required parameter: client_id")
        return

    # Atomic: SELECT + DELETE + INSERT inside one BEGIN IMMEDIATE tx.
    # `None` covers both "unknown refresh_token" and "client_id mismatch";
    # the unified ``invalid_grant`` response prevents leaking which case
    # occurred (no oracle for attackers fuzzing refresh tokens).
    rotated = await store.rotate_refresh_token(
        refresh_token=refresh,
        expected_client_id=client_id,
        ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
    )
    if rotated is None:
        await _send_error(send, 400, "invalid_grant", "Refresh token is invalid or has been used.")
        return

    await _send_token_response(send, rotated.token, rotated.refresh_token)


# ----------------------------------------------------------------------
# I/O helpers
# ----------------------------------------------------------------------


def _parse_request_body(scope: Scope, body: bytes) -> dict[str, str]:
    content_type = b""
    for name, value in scope.get("headers", []):
        if name == b"content-type":
            content_type = value
            break
    if content_type.startswith(b"application/json"):
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if isinstance(decoded, dict):
            return {str(k): str(v) for k, v in decoded.items()}
        return {}
    # Default: form-encoded (the spec-mandated format).
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        more = bool(message.get("more_body", False))
    return b"".join(chunks)


async def _send_token_response(send: Send, access_token: str, refresh_token: str | None) -> None:
    payload: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
    }
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    await _send_json(send, 200, payload)


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"pragma", b"no-cache"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": raw})


async def _send_error(send: Send, status: int, code: str, description: str) -> None:
    await _send_json(send, status, {"error": code, "error_description": description})
