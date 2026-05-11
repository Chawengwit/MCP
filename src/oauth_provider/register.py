"""POST /register — OAuth Dynamic Client Registration (RFC 7591).

Public clients only — no ``client_secret`` is issued, because Claude.ai
(the only practical caller today) runs in the browser and cannot store
one securely. The response carries ``client_id`` + the echoed
``redirect_uris`` + ``client_name``.

Redirect URI policy:
  - ``https://...`` URLs are always allowed.
  - ``http://...`` is only allowed when the host is a loopback literal
    (``127.0.0.1``, ``::1``, ``localhost``). This mirrors RFC 8252 §7.3
    "Loopback Interface Redirection".
  - Empty list, malformed URIs, or any other scheme is rejected with
    ``invalid_redirect_uri``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from starlette.types import Receive, Scope, Send

from .store import OAuthStore

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


async def register_handler(
    *,
    scope: Scope,
    receive: Receive,
    send: Send,
    store: OAuthStore,
) -> None:
    if scope.get("method", "") != "POST":
        await _send_error(send, 405, "method_not_allowed", "Use POST.")
        return

    body = await _read_body(receive)
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await _send_error(send, 400, "invalid_request", "Body is not valid JSON.")
        return

    if not isinstance(payload, dict):
        await _send_error(send, 400, "invalid_request", "Body must be a JSON object.")
        return

    raw_uris = payload.get("redirect_uris")
    client_name_raw = payload.get("client_name", "")
    client_name = str(client_name_raw).strip()[:200] if client_name_raw else "MCP Client"

    if not isinstance(raw_uris, list) or not raw_uris:
        await _send_error(
            send,
            400,
            "invalid_redirect_uri",
            "`redirect_uris` must be a non-empty list of strings.",
        )
        return

    validated: list[str] = []
    for uri in raw_uris:
        if not isinstance(uri, str):
            await _send_error(
                send,
                400,
                "invalid_redirect_uri",
                "Each entry in `redirect_uris` must be a string.",
            )
            return
        if not _is_acceptable_redirect_uri(uri):
            await _send_error(
                send,
                400,
                "invalid_redirect_uri",
                "Redirect URI scheme must be https, or http on a loopback host.",
            )
            return
        validated.append(uri)

    record = await store.register_client(client_name=client_name, redirect_uris=validated)
    response: dict[str, Any] = {
        "client_id": record.client_id,
        "client_id_issued_at": int(record.created_at.timestamp()),
        "client_name": record.client_name,
        "redirect_uris": record.redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    await _send_json(send, 201, response)


def _is_acceptable_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        return host in LOOPBACK_HOSTS
    return False


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


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _send_error(send: Send, status: int, code: str, description: str) -> None:
    await _send_json(send, status, {"error": code, "error_description": description})
