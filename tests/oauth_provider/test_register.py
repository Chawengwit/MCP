from __future__ import annotations

import json
from collections.abc import MutableMapping
from typing import Any

import pytest
from src.oauth_provider import OAuthStore
from src.oauth_provider.register import register_handler


class _Capture:
    def __init__(self) -> None:
        self.status: int = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.body: bytes = b""

    async def send(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def _scope(method: str, *, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {"type": "http", "method": method, "headers": headers or []}


async def _receive_body(body: bytes):
    sent = False

    async def _r() -> MutableMapping[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return _r


async def test_register_happy_path(store: OAuthStore) -> None:
    cap = _Capture()
    body = json.dumps(
        {"client_name": "ACME", "redirect_uris": ["https://acme.example.com/cb"]}
    ).encode()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(body),
        send=cap.send,
        store=store,
    )
    assert cap.status == 201
    parsed = json.loads(cap.body)
    assert parsed["client_name"] == "ACME"
    assert "client_id" in parsed
    assert parsed["redirect_uris"] == ["https://acme.example.com/cb"]
    assert parsed["token_endpoint_auth_method"] == "none"


async def test_register_rejects_invalid_scheme(store: OAuthStore) -> None:
    cap = _Capture()
    body = json.dumps({"client_name": "X", "redirect_uris": ["ftp://x"]}).encode()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(body),
        send=cap.send,
        store=store,
    )
    assert cap.status == 400
    parsed = json.loads(cap.body)
    assert parsed["error"] == "invalid_redirect_uri"


async def test_register_accepts_loopback_http(store: OAuthStore) -> None:
    cap = _Capture()
    body = json.dumps({"client_name": "C", "redirect_uris": ["http://127.0.0.1:5000/cb"]}).encode()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(body),
        send=cap.send,
        store=store,
    )
    assert cap.status == 201


async def test_register_rejects_non_loopback_http(store: OAuthStore) -> None:
    cap = _Capture()
    body = json.dumps({"client_name": "C", "redirect_uris": ["http://evil.example/cb"]}).encode()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(body),
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


async def test_register_rejects_empty_uris(store: OAuthStore) -> None:
    cap = _Capture()
    body = json.dumps({"client_name": "C", "redirect_uris": []}).encode()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(body),
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


async def test_register_rejects_get(store: OAuthStore) -> None:
    cap = _Capture()
    await register_handler(
        scope=_scope("GET"),
        receive=await _receive_body(b""),
        send=cap.send,
        store=store,
    )
    assert cap.status == 405


async def test_register_rejects_malformed_json(store: OAuthStore) -> None:
    cap = _Capture()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(b"{not json"),
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


@pytest.mark.parametrize("uris", [[123, "https://x"], "not-a-list", None])
async def test_register_rejects_invalid_types(store: OAuthStore, uris: Any) -> None:
    cap = _Capture()
    body = json.dumps({"client_name": "x", "redirect_uris": uris}).encode()
    await register_handler(
        scope=_scope("POST"),
        receive=await _receive_body(body),
        send=cap.send,
        store=store,
    )
    assert cap.status == 400
