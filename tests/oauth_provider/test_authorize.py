from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from src.config import ApiConfig
from src.oauth_provider import OAuthStore, ServiceSessionStore
from src.oauth_provider.authorize import authorize_get_handler, consent_post_handler
from src.oauth_provider.schemas import SessionInfo


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


def _get_scope(query: str) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/authorize",
        "query_string": query.encode(),
        "headers": [],
    }


def _post_scope(body_len: int) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/authorize/consent",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(body_len).encode("ascii")),
        ],
    }


async def _empty_receive() -> MutableMapping[str, Any]:
    return {"type": "http.disconnect"}


async def _body_receive(body: bytes):
    sent = False

    async def _r() -> MutableMapping[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return _r


async def _register_client(store: OAuthStore) -> tuple[str, str]:
    record = await store.register_client(
        client_name="Test Client",
        redirect_uris=["https://example.com/cb"],
    )
    return record.client_id, "https://example.com/cb"


async def test_authorize_renders_consent_form(store: OAuthStore) -> None:
    client_id, redirect = await _register_client(store)
    cap = _Capture()
    query = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=abc&code_challenge=chal&code_challenge_method=S256"
    )
    await authorize_get_handler(
        scope=_get_scope(query),
        receive=_empty_receive,
        send=cap.send,
        store=store,
    )
    assert cap.status == 200
    assert b"text/html" in (cap.header(b"content-type") or b"")
    assert b"Test Client" in cap.body
    assert b"api_key" in cap.body and b"secret_key" in cap.body
    # Hidden fields are escaped — no script tags from user input
    assert b"<script" not in cap.body


async def test_authorize_missing_param_returns_400(store: OAuthStore) -> None:
    cap = _Capture()
    await authorize_get_handler(
        scope=_get_scope("client_id=x"),
        receive=_empty_receive,
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


async def test_authorize_unknown_client_returns_400(store: OAuthStore) -> None:
    cap = _Capture()
    query = (
        "client_id=nope&redirect_uri=https://x/cb&response_type=code"
        "&state=s&code_challenge=c&code_challenge_method=S256"
    )
    await authorize_get_handler(
        scope=_get_scope(query),
        receive=_empty_receive,
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


async def test_authorize_unregistered_redirect_uri_returns_400(store: OAuthStore) -> None:
    client_id, _ = await _register_client(store)
    cap = _Capture()
    query = (
        f"client_id={client_id}&redirect_uri=https://attacker/cb&response_type=code"
        "&state=s&code_challenge=c&code_challenge_method=S256"
    )
    await authorize_get_handler(
        scope=_get_scope(query),
        receive=_empty_receive,
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


async def test_authorize_rejects_plain_pkce_method(store: OAuthStore) -> None:
    client_id, redirect = await _register_client(store)
    cap = _Capture()
    query = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=s&code_challenge=c&code_challenge_method=plain"
    )
    await authorize_get_handler(
        scope=_get_scope(query),
        receive=_empty_receive,
        send=cap.send,
        store=store,
    )
    assert cap.status == 400


async def test_authorize_html_escapes_state(store: OAuthStore) -> None:
    """A `state` containing `<script>` must be escaped in the HTML output."""
    client_id, redirect = await _register_client(store)
    cap = _Capture()
    evil_state = "%3Cscript%3Ealert%281%29%3C%2Fscript%3E"
    query = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        f"&state={evil_state}&code_challenge=c&code_challenge_method=S256"
    )
    await authorize_get_handler(
        scope=_get_scope(query),
        receive=_empty_receive,
        send=cap.send,
        store=store,
    )
    assert cap.status == 200
    assert b"<script>alert(1)</script>" not in cap.body
    assert b"&lt;script&gt;" in cap.body


async def test_consent_happy_path_redirects_with_code(
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    session_login_config: ApiConfig,
    fake_session: SessionInfo,
) -> None:
    client_id, redirect = await _register_client(store)
    body = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=abc&code_challenge=chal&code_challenge_method=S256"
        "&api_key=AKEY&secret_key=SKEY"
    ).encode()
    cap = _Capture()

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        assert api_key == "AKEY"
        assert secret_key == "SKEY"
        return fake_session

    await consent_post_handler(
        scope=_post_scope(len(body)),
        receive=await _body_receive(body),
        send=cap.send,
        store=store,
        service_session_store=service_session_store,
        authenticate=stub_authenticate,
        api_config=session_login_config,
    )
    assert cap.status == 302
    location = cap.header(b"location") or b""
    assert location.startswith(b"https://example.com/cb?")
    assert b"code=" in location
    assert b"state=abc" in location


async def test_consent_invalid_credentials_rerenders_form(
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    session_login_config: ApiConfig,
) -> None:
    from src.auth.credentials import AuthRequiredError

    client_id, redirect = await _register_client(store)
    body = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=abc&code_challenge=chal&code_challenge_method=S256"
        "&api_key=bad&secret_key=bad"
    ).encode()
    cap = _Capture()

    secret_internal_msg = "ZZZ-leak-marker-internal-rejected-msg-ZZZ"

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        raise AuthRequiredError(secret_internal_msg)

    await consent_post_handler(
        scope=_post_scope(len(body)),
        receive=await _body_receive(body),
        send=cap.send,
        store=store,
        service_session_store=service_session_store,
        authenticate=stub_authenticate,
        api_config=session_login_config,
    )
    assert cap.status == 401
    assert b"text/html" in (cap.header(b"content-type") or b"")
    assert secret_internal_msg.encode() not in cap.body  # internal message NOT leaked


async def test_consent_missing_field_returns_400(
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    session_login_config: ApiConfig,
    fake_session: SessionInfo,
) -> None:
    body = b"api_key=A&secret_key=B"  # no protocol params
    cap = _Capture()

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        return fake_session

    await consent_post_handler(
        scope=_post_scope(len(body)),
        receive=await _body_receive(body),
        send=cap.send,
        store=store,
        service_session_store=service_session_store,
        authenticate=stub_authenticate,
        api_config=session_login_config,
    )
    assert cap.status == 400
