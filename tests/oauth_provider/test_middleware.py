from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from src.oauth_provider import OAuthStore
from src.oauth_provider.middleware import (
    SCOPE_STATE_KEY,
    oauth_aware_bearer_middleware,
)


class _Recorder:
    def __init__(self) -> None:
        self.calls: int = 0
        self.last_scope: MutableMapping[str, Any] | None = None

    async def app(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        self.last_scope = scope


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

    def header(self, name: bytes) -> bytes | None:
        for n, v in self.headers:
            if n.lower() == name.lower():
                return v
        return None


def _scope(*, path: str = "/mcp", auth: bytes | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if auth is not None:
        headers.append((b"authorization", auth))
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
    }


async def _empty_receive() -> MutableMapping[str, Any]:
    return {"type": "http.disconnect"}


async def test_oauth_token_resolved_into_scope(store: OAuthStore) -> None:
    client = await store.register_client(client_name="c", redirect_uris=["https://x"])
    token = await store.save_access_token(client_id=client.client_id, user_id="user-7")
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="", issuer="https://mcp.example"
    )
    cap = _Capture()
    await mw(_scope(auth=f"Bearer {token.token}".encode()), _empty_receive, cap.send)
    assert rec.calls == 1
    assert rec.last_scope is not None
    mcp_user = rec.last_scope["state"][SCOPE_STATE_KEY]
    assert mcp_user["user_id"] == "user-7"
    assert mcp_user["auth_source"] == "oauth"


async def test_static_token_fallback(store: OAuthStore) -> None:
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="abc123", issuer="https://mcp.example"
    )
    cap = _Capture()
    await mw(_scope(auth=b"Bearer abc123"), _empty_receive, cap.send)
    assert rec.calls == 1
    assert rec.last_scope is not None
    mcp_user = rec.last_scope["state"][SCOPE_STATE_KEY]
    assert mcp_user["user_id"] is None
    assert mcp_user["auth_source"] == "static_bearer"


async def test_missing_auth_header_returns_401_with_www_authenticate(store: OAuthStore) -> None:
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="static", issuer="https://mcp.example.com"
    )
    cap = _Capture()
    await mw(_scope(), _empty_receive, cap.send)
    assert cap.status == 401
    assert rec.calls == 0
    www_auth = cap.header(b"www-authenticate") or b""
    assert b"resource_metadata=" in www_auth
    assert b"oauth-protected-resource" in www_auth
    assert b'error="invalid_token"' in www_auth


async def test_invalid_token_returns_401(store: OAuthStore) -> None:
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="static", issuer="https://mcp.example"
    )
    cap = _Capture()
    await mw(_scope(auth=b"Bearer not-a-real-token"), _empty_receive, cap.send)
    assert cap.status == 401
    assert rec.calls == 0


async def test_public_paths_pass_through(store: OAuthStore) -> None:
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="static", issuer="https://mcp.example"
    )
    cap = _Capture()
    await mw(_scope(path="/.well-known/oauth-authorization-server"), _empty_receive, cap.send)
    assert rec.calls == 1
    cap2 = _Capture()
    await mw(_scope(path="/register"), _empty_receive, cap2.send)
    assert rec.calls == 2


async def test_lifespan_scope_passes_through(store: OAuthStore) -> None:
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="static", issuer="https://mcp.example"
    )
    cap = _Capture()
    await mw({"type": "lifespan", "headers": []}, _empty_receive, cap.send)
    assert rec.calls == 1


async def test_401_body_contains_no_token_echo(store: OAuthStore) -> None:
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="abc", issuer="https://mcp.example"
    )
    cap = _Capture()
    await mw(_scope(auth=b"Bearer leaked-token-xyz"), _empty_receive, cap.send)
    assert b"leaked-token-xyz" not in cap.body


async def test_oauth_wins_when_oauth_and_static_both_valid(store: OAuthStore) -> None:
    """If a request's Bearer matches an OAuth-issued token AND happens to
    equal the configured static token, the OAuth lookup wins — auth_source
    must reflect the more-specific origin."""
    client = await store.register_client(client_name="mixed", redirect_uris=["https://x"])
    token = await store.save_access_token(client_id=client.client_id, user_id="user-mixed")
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app,
        store=store,
        static_token=token.token,  # contrived collision: same string
        issuer="https://mcp.example",
    )
    cap = _Capture()
    await mw(_scope(auth=f"Bearer {token.token}".encode()), _empty_receive, cap.send)
    assert rec.calls == 1
    assert rec.last_scope is not None
    mcp_user = rec.last_scope["state"][SCOPE_STATE_KEY]
    assert mcp_user["user_id"] == "user-mixed"
    assert mcp_user["auth_source"] == "oauth"


async def test_non_ascii_bearer_token_returns_401(store: OAuthStore) -> None:
    """Strict ASCII decode — a Bearer with non-ASCII bytes cannot match any
    token we issued (we only ever emit url-safe base64) and is rejected
    without a DB lookup."""
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="static", issuer="https://mcp.example"
    )
    cap = _Capture()
    # 0xff is invalid in ASCII; latin-1 happily decodes it. Strict mode rejects.
    await mw(_scope(auth=b"Bearer " + b"\xff" * 16), _empty_receive, cap.send)
    assert cap.status == 401
    assert rec.calls == 0


async def test_path_prefix_does_not_leak_to_unrelated_route(store: OAuthStore) -> None:
    """``/tokenizer`` must NOT bypass auth via the ``/token`` public prefix.
    Slash-boundary match prevents the leak."""
    rec = _Recorder()
    mw = oauth_aware_bearer_middleware(
        rec.app, store=store, static_token="static", issuer="https://mcp.example"
    )
    cap = _Capture()
    # No Authorization header; if /tokenizer were public the request would
    # pass through and rec.calls would be 1.
    await mw(_scope(path="/tokenizer"), _empty_receive, cap.send)
    assert cap.status == 401
    assert rec.calls == 0

    # Sanity: the exact-match path still passes through.
    cap2 = _Capture()
    rec2 = _Recorder()
    mw2 = oauth_aware_bearer_middleware(
        rec2.app, store=store, static_token="static", issuer="https://mcp.example"
    )
    await mw2(_scope(path="/token"), _empty_receive, cap2.send)
    assert rec2.calls == 1
