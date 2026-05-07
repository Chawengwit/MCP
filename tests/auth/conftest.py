from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import keyring
import keyring.errors
import pytest
from src.auth.oauth import OAuth, OAuthConfig, TokenInfo


@pytest.fixture
def mock_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Replace keyring.{get,set,delete}_password with an in-memory dict.

    Yields the backing dict so tests can inspect / pre-populate it.
    Keys are formatted as "<service>:<username>".
    """
    store: dict[str, str] = {}

    def _key(service: str, username: str) -> str:
        return f"{service}:{username}"

    def fake_get(service: str, username: str) -> str | None:
        return store.get(_key(service, username))

    def fake_set(service: str, username: str, password: str) -> None:
        store[_key(service, username)] = password

    def fake_delete(service: str, username: str) -> None:
        try:
            del store[_key(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError("not found") from exc

    monkeypatch.setattr(keyring, "get_password", fake_get)
    monkeypatch.setattr(keyring, "set_password", fake_set)
    monkeypatch.setattr(keyring, "delete_password", fake_delete)
    return store


@pytest.fixture
def make_token_response() -> Callable[..., dict[str, Any]]:
    """Factory for fake provider token-endpoint JSON responses."""

    def _make(
        *,
        access_token: str = "fake_access_token",
        refresh_token: str | None = "fake_refresh_token",
        expires_in: int = 3600,
        token_type: str = "Bearer",
        scope: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "access_token": access_token,
            "token_type": token_type,
            "expires_in": expires_in,
        }
        if refresh_token is not None:
            body["refresh_token"] = refresh_token
        if scope is not None:
            body["scope"] = scope
        return body

    return _make


@pytest.fixture
def mock_httpx_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    make_token_response: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Patch httpx.AsyncClient so token-endpoint POSTs return a canned JSON body.

    Returns a dict the test can mutate to control the next response or inspect
    the captured request: {"response": <json>, "status_code": 200,
    "calls": [<httpx.Request>...], "request_data": <last form data>}.
    """
    state: dict[str, Any] = {
        "response": make_token_response(),
        "status_code": 200,
        "calls": [],
        "request_data": None,
    }

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(
            self, url: str, *, data: dict[str, Any] | None = None, **kwargs: Any
        ) -> httpx.Response:
            state["calls"].append({"url": url, "data": data})
            state["request_data"] = data
            return httpx.Response(
                status_code=state["status_code"],
                json=state["response"],
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("src.auth.oauth.httpx.AsyncClient", _FakeAsyncClient)
    return state


@pytest.fixture
def mock_webbrowser(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub webbrowser.open so tests don't actually open a browser.

    Returns a dict capturing whether/with-what-URL it was called.
    """
    state: dict[str, Any] = {"called": False, "url": None, "new": None}

    def fake_open(url: str, new: int = 0, autoraise: bool = True) -> bool:
        state["called"] = True
        state["url"] = url
        state["new"] = new
        return True

    monkeypatch.setattr("src.auth.oauth.webbrowser.open", fake_open)
    return state


@pytest.fixture
def patched_callback_server(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace OAuth._run_callback_server with a stub returning a pre-set code.

    Tests set state["code"] (and optionally state["raise"]) BEFORE invoking
    start_flow. The stub records the expected_state arg so tests can verify
    that PKCE state was generated and threaded through correctly.
    """
    state: dict[str, Any] = {
        "code": "fake_auth_code",
        "raise": None,
        "received_state": None,
        "received_port": None,
    }

    async def fake_run(self: OAuth, port: int, expected_state: str) -> str:
        state["received_state"] = expected_state
        state["received_port"] = port
        if state["raise"] is not None:
            raise state["raise"]
        return state["code"]

    monkeypatch.setattr(OAuth, "_run_callback_server", fake_run)
    return state


@pytest.fixture
def oauth_config() -> OAuthConfig:
    return OAuthConfig(
        provider="example",
        client_id="client_xyz",
        client_secret="secret_abc",
        authorize_url="https://example.com/oauth/authorize",
        token_url="https://example.com/oauth/token",
        scopes=["read", "write"],
    )


@pytest.fixture
def fresh_token() -> TokenInfo:
    """A TokenInfo that is comfortably non-expired (1 hour from now)."""
    return TokenInfo(
        access_token="fresh_access_token_xyz",
        refresh_token="fresh_refresh_token_xyz",
        expires_at=time.time() + 3600,
    )


@pytest.fixture
def expiring_token() -> TokenInfo:
    """A TokenInfo within the 5-minute refresh buffer."""
    return TokenInfo(
        access_token="expiring_access_token",
        refresh_token="valid_refresh_token",
        expires_at=time.time() + 60,  # 1 minute remaining → triggers refresh
    )


@pytest.fixture
def stderr_capture(capfd: pytest.CaptureFixture[str]) -> Iterator[pytest.CaptureFixture[str]]:
    """Convenience wrapper around capfd for tests that grep stderr for secrets."""
    yield capfd
