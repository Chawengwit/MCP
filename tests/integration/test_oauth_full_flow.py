"""End-to-end OAuth Provider flow via Starlette TestClient.

Exercises: discovery → register → authorize → consent → token → /mcp.
The Service API is stubbed; the MCP `/mcp` endpoint is replaced by a tiny
ASGI echo that confirms the resolved user_id reached the inner app.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from src.config import ApiAuthConfig, ApiConfig
from src.oauth_provider import Encryptor, OAuthStore, ServiceSessionStore
from src.oauth_provider.middleware import (
    SCOPE_STATE_KEY,
    oauth_aware_bearer_middleware,
)
from src.oauth_provider.pkce import derive_challenge
from src.oauth_provider.routes import build_oauth_dispatcher
from src.oauth_provider.schemas import SessionInfo
from starlette.testclient import TestClient


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def issuer() -> str:
    return "https://mcp.example.com"


@pytest.fixture
def service_api_config() -> ApiConfig:
    return ApiConfig(
        type="rest",
        base_url="https://svc.example.com",
        auth=ApiAuthConfig(
            type="session_login",
            login_path="/v1/auth/login",
            login_method="POST",
            credentials={"api_key": "{api_key}", "secret_key": "{secret_key}"},
            session_id_field="session_id",
            session_expire_field="session_expire",
            user_id_field="user_id",
            session_header="X-Session-Id",
            session_format="{session_id}",
        ),
    )


@pytest.fixture
async def app(
    tmp_path: Path,
    fernet_key: str,
    issuer: str,
    service_api_config: ApiConfig,
):
    """Compose middleware + dispatcher + a stub `/mcp` app."""
    store = OAuthStore(tmp_path / "oauth.db")
    await store.init_db()
    encryptor = Encryptor(fernet_key.encode("ascii"))

    fresh_session = SessionInfo(
        user_id="user-9",
        session_id="svc-sid",
        session_expire=1_900_000_000,
    )

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        if api_key == "GOOD" and secret_key == "GOOD":
            return fresh_session
        raise __import__("src.auth.credentials", fromlist=["AuthRequiredError"]).AuthRequiredError(
            "rejected"
        )

    sstore = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=service_api_config,
        authenticate=stub_authenticate,
    )

    dispatcher = build_oauth_dispatcher(
        store=store,
        service_session_store=sstore,
        authenticate=stub_authenticate,
        api_config=service_api_config,
        issuer=issuer,
    )

    async def mcp_echo(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        mcp_user = scope.get("state", {}).get(SCOPE_STATE_KEY, {})
        body = json.dumps({"user_id": mcp_user.get("user_id")}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def outer(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        handled = await dispatcher(scope, receive, send)
        if handled:
            return
        if scope.get("path") == "/mcp":
            await mcp_echo(scope, receive, send)
            return
        # 404 fallback for unknown paths
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"not_found"}'})

    return oauth_aware_bearer_middleware(outer, store=store, static_token="", issuer=issuer)


async def test_full_flow(app, issuer: str) -> None:
    client = TestClient(app)
    # 1) Discovery
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta["issuer"] == issuer
    assert meta["code_challenge_methods_supported"] == ["S256"]

    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json()["authorization_servers"] == [issuer]

    # 2) Dynamic registration
    r = client.post(
        "/register",
        json={"client_name": "ACME", "redirect_uris": ["https://acme.example.com/cb"]},
    )
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]

    # 3) /mcp without token => 401
    r = client.post("/mcp", json={"x": 1})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]

    # 4) Authorize
    verifier = "v" * 43
    challenge = derive_challenge(verifier)
    r = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://acme.example.com/cb",
            "response_type": "code",
            "state": "st",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 200
    assert b"ACME" in r.content

    # 5) Consent
    r = client.post(
        "/authorize/consent",
        data={
            "client_id": client_id,
            "redirect_uri": "https://acme.example.com/cb",
            "response_type": "code",
            "state": "st",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "api_key": "GOOD",
            "secret_key": "GOOD",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    # extract `code=` value
    code = location.split("code=")[1].split("&")[0]

    # 6) Token exchange
    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://acme.example.com/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["token_type"] == "Bearer"

    # 7) Call /mcp with the access token
    r = client.post(
        "/mcp",
        headers={"authorization": f"Bearer {tokens['access_token']}"},
        json={"x": 1},
    )
    assert r.status_code == 200
    assert r.json() == {"user_id": "user-9"}

    # 8) Refresh
    r = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    assert r.status_code == 200
    new_tokens = r.json()
    assert new_tokens["access_token"] != tokens["access_token"]


async def test_full_flow_rejects_bad_pkce(app) -> None:
    client = TestClient(app)
    r = client.post(
        "/register",
        json={"client_name": "ACME", "redirect_uris": ["https://acme.example.com/cb"]},
    )
    client_id = r.json()["client_id"]

    verifier = "v" * 43
    challenge = derive_challenge(verifier)
    r = client.post(
        "/authorize/consent",
        data={
            "client_id": client_id,
            "redirect_uri": "https://acme.example.com/cb",
            "response_type": "code",
            "state": "st",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "api_key": "GOOD",
            "secret_key": "GOOD",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    code = r.headers["location"].split("code=")[1].split("&")[0]

    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://acme.example.com/cb",
            "client_id": client_id,
            "code_verifier": "wrong-verifier-but-43-chars-long-padding-aaa",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
