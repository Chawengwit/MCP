"""Service API success / failure paths in the consent POST handler.

`tests/oauth_provider/test_authorize.py` exercises form parsing and
validation; this file focuses on the Service-API integration surface.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from src.auth.credentials import AuthError, AuthRequiredError
from src.config import ApiConfig
from src.oauth_provider import OAuthStore, ServiceSessionStore
from src.oauth_provider.authorize import consent_post_handler
from src.oauth_provider.schemas import SessionInfo


class _Capture:
    def __init__(self) -> None:
        self.status: int = 0
        self.body: bytes = b""
        self.headers: list[tuple[bytes, bytes]] = []

    async def send(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def _scope(body_len: int) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/authorize/consent",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
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


async def _seed(store: OAuthStore) -> tuple[str, str]:
    rec = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    return rec.client_id, "https://x/cb"


async def test_consent_persists_session_on_success(
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    session_login_config: ApiConfig,
    fake_session: SessionInfo,
) -> None:
    client_id, redirect = await _seed(store)
    body = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=abc&code_challenge=chal&code_challenge_method=S256"
        "&api_key=AKEY&secret_key=SKEY"
    ).encode()

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        return fake_session

    cap = _Capture()
    await consent_post_handler(
        scope=_scope(len(body)),
        receive=await _receive(body),
        send=cap.send,
        store=store,
        service_session_store=service_session_store,
        authenticate=stub_authenticate,
        api_config=session_login_config,
    )
    assert cap.status == 302
    stored = await store.get_service_session(fake_session.user_id)
    assert stored is not None
    # Stored credentials are ciphertext — plaintext does not appear.
    assert b"AKEY" not in stored.encrypted_api_key
    assert b"SKEY" not in stored.encrypted_secret_key


async def test_consent_service_api_5xx_returns_502_html(
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    session_login_config: ApiConfig,
) -> None:
    client_id, redirect = await _seed(store)
    body = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=abc&code_challenge=chal&code_challenge_method=S256"
        "&api_key=A&secret_key=B"
    ).encode()

    internal_msg = "ZZZ-internal-upstream-blew-up-marker-ZZZ"

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        raise AuthError(internal_msg)

    cap = _Capture()
    await consent_post_handler(
        scope=_scope(len(body)),
        receive=await _receive(body),
        send=cap.send,
        store=store,
        service_session_store=service_session_store,
        authenticate=stub_authenticate,
        api_config=session_login_config,
    )
    assert cap.status == 502
    assert internal_msg.encode() not in cap.body  # original error not echoed


async def test_consent_does_not_leak_credentials_in_html_response(
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    session_login_config: ApiConfig,
) -> None:
    """The re-rendered consent form must not echo back the submitted secret_key."""
    client_id, redirect = await _seed(store)
    secret = "SUPER-SENSITIVE-SECRET-KEY"
    body = (
        f"client_id={client_id}&redirect_uri={redirect}&response_type=code"
        "&state=abc&code_challenge=chal&code_challenge_method=S256"
        f"&api_key=A&secret_key={secret}"
    ).encode()

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        raise AuthRequiredError("bad creds")

    cap = _Capture()
    await consent_post_handler(
        scope=_scope(len(body)),
        receive=await _receive(body),
        send=cap.send,
        store=store,
        service_session_store=service_session_store,
        authenticate=stub_authenticate,
        api_config=session_login_config,
    )
    assert cap.status == 401
    assert secret.encode("utf-8") not in cap.body
