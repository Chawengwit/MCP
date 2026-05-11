from __future__ import annotations

import json
from collections.abc import MutableMapping
from typing import Any

import pytest
from src.oauth_provider import OAuthStore
from src.oauth_provider.pkce import derive_challenge
from src.oauth_provider.token import token_handler


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


def _scope(content_type: bytes = b"application/x-www-form-urlencoded") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/token",
        "headers": [(b"content-type", content_type)],
    }


async def _receive(body: bytes):
    sent = False

    async def _r() -> MutableMapping[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return _r


async def _seed(store: OAuthStore, verifier: str) -> tuple[str, str, str]:
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    code_record = await store.save_authorization_code(
        client_id=client.client_id,
        user_id="user-1",
        redirect_uri="https://x/cb",
        code_challenge=derive_challenge(verifier),
    )
    return client.client_id, code_record.code, "https://x/cb"


async def test_authorization_code_grant_happy_path(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, redirect = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri={redirect}"
        f"&client_id={client_id}&code_verifier={verifier}"
    ).encode()
    cap = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap.send, store=store)
    assert cap.status == 200
    parsed = json.loads(cap.body)
    assert parsed["token_type"] == "Bearer"
    assert "access_token" in parsed
    assert "refresh_token" in parsed
    assert "expires_in" in parsed


async def test_authorization_code_grant_rejects_used_code(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, redirect = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri={redirect}"
        f"&client_id={client_id}&code_verifier={verifier}"
    ).encode()
    cap1 = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap1.send, store=store)
    cap2 = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap2.send, store=store)
    assert cap1.status == 200
    assert cap2.status == 400
    assert json.loads(cap2.body)["error"] == "invalid_grant"


async def test_authorization_code_grant_rejects_bad_pkce(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, redirect = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri={redirect}"
        f"&client_id={client_id}&code_verifier=wrong-verifier-still-43-chars-long-x"
    ).encode()
    cap = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap.send, store=store)
    assert cap.status == 400
    assert json.loads(cap.body)["error"] == "invalid_grant"


async def test_authorization_code_grant_rejects_redirect_uri_mismatch(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, _ = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri=https://attacker/cb"
        f"&client_id={client_id}&code_verifier={verifier}"
    ).encode()
    cap = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap.send, store=store)
    assert cap.status == 400
    assert json.loads(cap.body)["error"] == "invalid_grant"


async def test_authorization_code_grant_rejects_wrong_client(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, redirect = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri={redirect}"
        f"&client_id=wrong-client&code_verifier={verifier}"
    ).encode()
    cap = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap.send, store=store)
    assert cap.status == 400


async def test_refresh_token_grant_rotates(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, redirect = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri={redirect}"
        f"&client_id={client_id}&code_verifier={verifier}"
    ).encode()
    cap1 = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap1.send, store=store)
    first = json.loads(cap1.body)
    refresh_body = (
        f"grant_type=refresh_token&refresh_token={first['refresh_token']}&client_id={client_id}"
    ).encode()
    cap2 = _Capture()
    await token_handler(
        scope=_scope(), receive=await _receive(refresh_body), send=cap2.send, store=store
    )
    assert cap2.status == 200
    second = json.loads(cap2.body)
    assert second["access_token"] != first["access_token"]
    # Used refresh token is single-use.
    cap3 = _Capture()
    await token_handler(
        scope=_scope(), receive=await _receive(refresh_body), send=cap3.send, store=store
    )
    assert cap3.status == 400


async def test_unsupported_grant_type(store: OAuthStore) -> None:
    cap = _Capture()
    await token_handler(
        scope=_scope(),
        receive=await _receive(b"grant_type=client_credentials"),
        send=cap.send,
        store=store,
    )
    assert cap.status == 400
    assert json.loads(cap.body)["error"] == "unsupported_grant_type"


async def test_get_method_rejected(store: OAuthStore) -> None:
    cap = _Capture()
    bad_scope = {"type": "http", "method": "GET", "path": "/token", "headers": []}
    await token_handler(scope=bad_scope, receive=await _receive(b""), send=cap.send, store=store)
    assert cap.status == 405


async def test_token_response_has_no_cache_headers(store: OAuthStore) -> None:
    verifier = "v" * 43
    client_id, code, redirect = await _seed(store, verifier)
    body = (
        f"grant_type=authorization_code&code={code}&redirect_uri={redirect}"
        f"&client_id={client_id}&code_verifier={verifier}"
    ).encode()
    cap = _Capture()
    await token_handler(scope=_scope(), receive=await _receive(body), send=cap.send, store=store)
    cache_control = next((v for n, v in cap.headers if n == b"cache-control"), b"")
    assert b"no-store" in cache_control


# ----------------------------------------------------------------------
# MCP_OAUTH_ACCESS_TOKEN_TTL env var resolution
# ----------------------------------------------------------------------


class TestAccessTokenTTLResolution:
    """``_resolve_access_token_ttl`` reads ``MCP_OAUTH_ACCESS_TOKEN_TTL`` once
    at import time. Misconfiguration must fall back to the default rather
    than crashing boot — operators discover the issue via the stderr warning
    while the server keeps working."""

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.oauth_provider.token import (
            _DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
            _resolve_access_token_ttl,
        )

        monkeypatch.delenv("MCP_OAUTH_ACCESS_TOKEN_TTL", raising=False)
        assert _resolve_access_token_ttl() == _DEFAULT_ACCESS_TOKEN_TTL_SECONDS

    def test_valid_positive_int_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.oauth_provider.token import _resolve_access_token_ttl

        monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_TTL", "120")
        assert _resolve_access_token_ttl() == 120

    def test_blank_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.oauth_provider.token import (
            _DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
            _resolve_access_token_ttl,
        )

        monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_TTL", "   ")
        assert _resolve_access_token_ttl() == _DEFAULT_ACCESS_TOKEN_TTL_SECONDS

    def test_non_integer_falls_back_to_default_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from src.oauth_provider.token import (
            _DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
            _resolve_access_token_ttl,
        )

        monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_TTL", "not-a-number")
        assert _resolve_access_token_ttl() == _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
        # Warning must go to stderr (CLAUDE.md rule), not stdout.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "MCP_OAUTH_ACCESS_TOKEN_TTL" in captured.err

    def test_non_positive_falls_back_to_default_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from src.oauth_provider.token import (
            _DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
            _resolve_access_token_ttl,
        )

        monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_TTL", "0")
        assert _resolve_access_token_ttl() == _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "must be > 0" in captured.err

        monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_TTL", "-5")
        assert _resolve_access_token_ttl() == _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
