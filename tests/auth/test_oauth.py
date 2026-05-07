from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import socket
import time
import urllib.parse
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from src.auth.oauth import (
    OAuth,
    OAuthConfig,
    OAuthError,
    TokenInfo,
    _build_authorize_url,
    _generate_pkce,
    _parse_expires_in,
)

# ---------------------------------------------------------------------------
# PKCE primitives
# ---------------------------------------------------------------------------


def test_generate_pkce_verifier_length_in_range() -> None:
    verifier, _, _ = _generate_pkce()
    assert 43 <= len(verifier) <= 128


def test_generate_pkce_challenge_is_b64url_sha256_of_verifier() -> None:
    verifier, challenge, _ = _generate_pkce()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_generate_pkce_states_are_unique_per_call() -> None:
    states = {_generate_pkce()[2] for _ in range(20)}
    assert len(states) == 20  # every call produces a new state


def test_generate_pkce_verifier_uses_rfc_safe_alphabet() -> None:
    verifier, _, _ = _generate_pkce()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(verifier) <= allowed


# ---------------------------------------------------------------------------
# Authorize URL builder
# ---------------------------------------------------------------------------


def test_build_authorize_url_includes_all_required_params(oauth_config: OAuthConfig) -> None:
    url = _build_authorize_url(
        config=oauth_config,
        challenge="ch_abc",
        state="st_xyz",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["response_type"] == "code"
    assert params["client_id"] == "client_xyz"
    assert params["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert params["scope"] == "read write"
    assert params["state"] == "st_xyz"
    assert params["code_challenge"] == "ch_abc"
    assert params["code_challenge_method"] == "S256"


def test_build_authorize_url_appends_to_existing_query(oauth_config: OAuthConfig) -> None:
    cfg = oauth_config.model_copy(
        update={"authorize_url": "https://example.com/oauth/authorize?foo=bar"}
    )
    url = _build_authorize_url(
        config=cfg, challenge="c", state="s", redirect_uri="http://127.0.0.1:8765/callback"
    )
    assert "foo=bar" in url
    assert "&response_type=code" in url


# ---------------------------------------------------------------------------
# Callback server — real loopback socket
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait_for_port(port: int, *, timeout: float = 2.0) -> None:
    """Poll TCP connect to 127.0.0.1:port until it accepts, or raise."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except (ConnectionRefusedError, OSError) as exc:
            last_exc = exc
            await asyncio.sleep(0.01)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise TimeoutError(f"port {port} not accepting connections after {timeout}s") from last_exc


async def _send_callback_request(
    port: int, *, code: str | None, state: str | None
) -> httpx.Response:
    params: dict[str, str] = {}
    if code is not None:
        params["code"] = code
    if state is not None:
        params["state"] = state
    qs = urllib.parse.urlencode(params)
    async with httpx.AsyncClient(timeout=2.0) as client:
        return await client.get(f"http://127.0.0.1:{port}/callback?{qs}")


async def test_callback_server_returns_code_on_valid_state() -> None:
    port = _free_port()
    oauth = OAuth(callback_port=port)
    server_task = asyncio.create_task(oauth._run_callback_server(port, expected_state="good_state"))
    await _wait_for_port(port)
    response = await _send_callback_request(port, code="auth_code_42", state="good_state")
    assert response.status_code == 200
    assert "complete" in response.text.lower()

    result = await asyncio.wait_for(server_task, timeout=2.0)
    assert result == "auth_code_42"


async def test_callback_server_rejects_state_mismatch() -> None:
    port = _free_port()
    oauth = OAuth(callback_port=port)
    server_task = asyncio.create_task(oauth._run_callback_server(port, expected_state="good_state"))
    await _wait_for_port(port)
    response = await _send_callback_request(port, code="ignored", state="wrong_state")
    assert response.status_code == 400

    with pytest.raises(OAuthError):
        await asyncio.wait_for(server_task, timeout=2.0)


async def test_callback_server_rejects_missing_code() -> None:
    port = _free_port()
    oauth = OAuth(callback_port=port)
    server_task = asyncio.create_task(oauth._run_callback_server(port, expected_state="good_state"))
    await _wait_for_port(port)
    response = await _send_callback_request(port, code=None, state="good_state")
    assert response.status_code == 400

    with pytest.raises(OAuthError):
        await asyncio.wait_for(server_task, timeout=2.0)


async def test_callback_server_binds_to_127_0_0_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify host argument passed to asyncio.start_server is exactly 127.0.0.1."""
    captured_host: dict[str, Any] = {}
    original = asyncio.start_server

    async def spy_start_server(*args: Any, **kwargs: Any) -> Any:
        captured_host["host"] = kwargs.get("host")
        return await original(*args, **kwargs)

    monkeypatch.setattr("src.auth.oauth.asyncio.start_server", spy_start_server)

    port = _free_port()
    oauth = OAuth(callback_port=port)
    server_task = asyncio.create_task(oauth._run_callback_server(port, expected_state="s"))
    await _wait_for_port(port)
    # Send a valid request so the server returns and the task completes cleanly.
    await _send_callback_request(port, code="c", state="s")
    await asyncio.wait_for(server_task, timeout=2.0)

    assert captured_host["host"] == "127.0.0.1"


async def test_callback_server_port_in_use_raises_oauth_error() -> None:
    port = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        oauth = OAuth(callback_port=port)
        with pytest.raises(OAuthError, match="callback port"):
            await oauth._run_callback_server(port, expected_state="s")
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# Token exchange + refresh
# ---------------------------------------------------------------------------


async def test_exchange_code_posts_expected_form(
    mock_httpx_token_endpoint: dict[str, Any],
    oauth_config: OAuthConfig,
) -> None:
    oauth = OAuth(callback_port=8765)
    token = await oauth._exchange_code(
        config=oauth_config,
        code="the_code",
        verifier="the_verifier",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    sent = mock_httpx_token_endpoint["request_data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the_code"
    assert sent["client_id"] == "client_xyz"
    assert sent["client_secret"] == "secret_abc"
    assert sent["code_verifier"] == "the_verifier"
    assert sent["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert isinstance(token, TokenInfo)
    assert token.access_token == "fake_access_token"
    assert token.refresh_token == "fake_refresh_token"
    # expires_at = now + 3600 (default fixture); allow 5s slop.
    assert abs(token.expires_at - (time.time() + 3600)) < 5


async def test_refresh_posts_refresh_grant(
    mock_httpx_token_endpoint: dict[str, Any],
    oauth_config: OAuthConfig,
) -> None:
    oauth = OAuth(callback_port=8765)
    token = await oauth.refresh(oauth_config, refresh_token="rt_999")
    sent = mock_httpx_token_endpoint["request_data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "rt_999"
    assert sent["client_id"] == "client_xyz"
    assert sent["client_secret"] == "secret_abc"
    assert isinstance(token, TokenInfo)


async def test_token_endpoint_error_raises_oauth_error(
    mock_httpx_token_endpoint: dict[str, Any],
    oauth_config: OAuthConfig,
) -> None:
    mock_httpx_token_endpoint["status_code"] = 401
    mock_httpx_token_endpoint["response"] = {"error": "invalid_client"}
    oauth = OAuth(callback_port=8765)
    with pytest.raises(OAuthError, match="HTTP 401"):
        await oauth.refresh(oauth_config, refresh_token="rt")


async def test_token_endpoint_missing_access_token_raises(
    mock_httpx_token_endpoint: dict[str, Any],
    oauth_config: OAuthConfig,
) -> None:
    mock_httpx_token_endpoint["response"] = {"token_type": "Bearer", "expires_in": 3600}
    oauth = OAuth(callback_port=8765)
    with pytest.raises(OAuthError, match="access_token"):
        await oauth.refresh(oauth_config, refresh_token="rt")


# ---------------------------------------------------------------------------
# Full start_flow happy path (using patched callback server fixture)
# ---------------------------------------------------------------------------


async def test_start_flow_happy_path(
    mock_httpx_token_endpoint: dict[str, Any],
    mock_webbrowser: dict[str, Any],
    patched_callback_server: dict[str, Any],
    oauth_config: OAuthConfig,
) -> None:
    patched_callback_server["code"] = "received_code_123"

    oauth = OAuth(callback_port=8765)
    token = await oauth.start_flow(oauth_config)

    # 1. Browser was opened with the authorize URL.
    assert mock_webbrowser["called"] is True
    assert mock_webbrowser["url"].startswith("https://example.com/oauth/authorize")
    assert "code_challenge_method=S256" in mock_webbrowser["url"]

    # 2. Callback fixture observed the same state generated for the URL.
    parsed = urllib.parse.urlparse(mock_webbrowser["url"])
    state_in_url = dict(urllib.parse.parse_qsl(parsed.query))["state"]
    assert patched_callback_server["received_state"] == state_in_url
    assert patched_callback_server["received_port"] == 8765

    # 3. Token endpoint received the captured code.
    assert mock_httpx_token_endpoint["request_data"]["code"] == "received_code_123"

    # 4. Returned TokenInfo is sane.
    assert isinstance(token, TokenInfo)
    assert token.access_token == "fake_access_token"


# ---------------------------------------------------------------------------
# Secret-omission: tokens never appear in stderr/log output
# ---------------------------------------------------------------------------


async def test_no_secrets_in_logs(
    caplog: pytest.LogCaptureFixture,
    mock_httpx_token_endpoint: dict[str, Any],
    mock_webbrowser: dict[str, Any],
    patched_callback_server: dict[str, Any],
    oauth_config: OAuthConfig,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Run a full flow and ensure no token/secret values appear in any log line."""
    caplog.set_level("DEBUG", logger="mcp.auth")
    mock_httpx_token_endpoint["response"] = {
        "access_token": "SECRET_ACCESS_TOKEN_12345",
        "refresh_token": "SECRET_REFRESH_TOKEN_67890",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    patched_callback_server["code"] = "secret_auth_code"

    oauth = OAuth(callback_port=8765)
    await oauth.start_flow(oauth_config)

    captured = capfd.readouterr()
    full_output = caplog.text + captured.out + captured.err
    forbidden = [
        "SECRET_ACCESS_TOKEN_12345",
        "SECRET_REFRESH_TOKEN_67890",
        "secret_abc",  # client_secret
    ]
    for needle in forbidden:
        assert needle not in full_output, f"leaked {needle!r} into logs/stdout/stderr"


# ---------------------------------------------------------------------------
# repr() does not leak secret values
# ---------------------------------------------------------------------------


def test_oauth_config_repr_omits_client_secret() -> None:
    config = OAuthConfig(
        provider="example",
        client_id="client_xyz",
        client_secret="HIGHLY_SECRET_VALUE_42",
        authorize_url="https://example.com/oauth/authorize",
        token_url="https://example.com/oauth/token",
        scopes=["read"],
    )
    text = repr(config)
    assert "HIGHLY_SECRET_VALUE_42" not in text
    assert "client_secret" not in text  # field name itself is hidden by repr=False


def test_token_info_repr_omits_access_and_refresh_tokens() -> None:
    token = TokenInfo(
        access_token="OBSERVABLE_ACCESS_TOKEN_12345",
        refresh_token="OBSERVABLE_REFRESH_TOKEN_67890",
        expires_at=time.time() + 3600,
    )
    text = repr(token)
    assert "OBSERVABLE_ACCESS_TOKEN_12345" not in text
    assert "OBSERVABLE_REFRESH_TOKEN_67890" not in text
    # Non-secret fields remain visible — caller can still see expiry/type.
    assert "expires_at" in text
    assert "Bearer" in text


def test_token_info_str_format_omits_secrets() -> None:
    token = TokenInfo(
        access_token="STR_FORMAT_LEAK_ACCESS",
        refresh_token="STR_FORMAT_LEAK_REFRESH",
        expires_at=time.time() + 3600,
    )
    formatted = f"{token}"
    assert "STR_FORMAT_LEAK_ACCESS" not in formatted
    assert "STR_FORMAT_LEAK_REFRESH" not in formatted


# ---------------------------------------------------------------------------
# HTTPS enforcement on authorize_url / token_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("authorize_url", "http://example.com/oauth/authorize"),
        ("token_url", "http://example.com/oauth/token"),
        ("authorize_url", "ftp://example.com/oauth/authorize"),
    ],
)
def test_oauth_config_rejects_non_https_urls(field: str, bad_value: str) -> None:
    base: dict[str, Any] = {
        "provider": "example",
        "client_id": "x",
        "client_secret": "y",
        "authorize_url": "https://example.com/oauth/authorize",
        "token_url": "https://example.com/oauth/token",
        "scopes": ["read"],
    }
    base[field] = bad_value
    with pytest.raises(ValidationError, match="https"):
        OAuthConfig(**base)


def test_oauth_config_accepts_https_urls() -> None:
    config = OAuthConfig(
        provider="example",
        client_id="x",
        client_secret="y",
        authorize_url="https://example.com/oauth/authorize",
        token_url="https://example.com/oauth/token",
        scopes=["read"],
    )
    assert config.authorize_url.startswith("https://")
    assert config.token_url.startswith("https://")


# ---------------------------------------------------------------------------
# expires_in parsing — accepts numeric and string forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (3600, 3600.0),
        (3600.0, 3600.0),
        ("3600", 3600.0),
        ("7200", 7200.0),
        (None, 3600.0),  # default
        ("not_a_number", 3600.0),  # default
        (True, 3600.0),  # bool is rejected (would otherwise be 1.0)
    ],
)
def test_parse_expires_in_accepts_various_forms(value: Any, expected: float) -> None:
    assert _parse_expires_in(value) == expected


async def test_token_endpoint_accepts_string_expires_in(
    mock_httpx_token_endpoint: dict[str, Any],
    oauth_config: OAuthConfig,
) -> None:
    mock_httpx_token_endpoint["response"] = {
        "access_token": "abc",
        "refresh_token": "rt",
        "expires_in": "7200",  # string, not int
        "token_type": "Bearer",
    }
    oauth = OAuth(callback_port=8765)
    token = await oauth.refresh(oauth_config, refresh_token="rt")
    # 7200 seconds from now (allow 5s slop).
    assert abs(token.expires_at - (time.time() + 7200)) < 5
