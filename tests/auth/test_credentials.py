from __future__ import annotations

import asyncio
import time
from typing import Any

import keyring
import keyring.errors
import pytest
from src.auth.credentials import (
    AuthRequiredError,
    Credentials,
    CredentialStorageError,
)
from src.auth.oauth import OAuth, OAuthConfig, TokenInfo


@pytest.fixture
def credentials(
    mock_keyring: dict[str, str],
    oauth_config: OAuthConfig,
) -> Credentials:
    return Credentials(
        oauth=OAuth(callback_port=8765),
        oauth_configs={"example": oauth_config},
    )


# ---------------------------------------------------------------------------
# get / peek / store / clear roundtrip
# ---------------------------------------------------------------------------


async def test_store_and_get_roundtrip(credentials: Credentials, fresh_token: TokenInfo) -> None:
    await credentials.store("example", fresh_token)
    got = await credentials.get("example")
    assert got is not None
    assert got.access_token == fresh_token.access_token
    assert got.refresh_token == fresh_token.refresh_token


async def test_peek_returns_stored_without_side_effects(
    credentials: Credentials,
    fresh_token: TokenInfo,
    mock_httpx_token_endpoint: dict[str, Any],
) -> None:
    await credentials.store("example", fresh_token)
    peeked = await credentials.peek("example")
    assert peeked is not None
    assert peeked.access_token == fresh_token.access_token
    # peek must not call the token endpoint, even if expired.
    assert mock_httpx_token_endpoint["calls"] == []


async def test_peek_returns_none_when_not_stored(credentials: Credentials) -> None:
    assert await credentials.peek("nope") is None


async def test_peek_does_not_refresh_expired_token(
    credentials: Credentials,
    expiring_token: TokenInfo,
    mock_httpx_token_endpoint: dict[str, Any],
) -> None:
    await credentials.store("example", expiring_token)
    peeked = await credentials.peek("example")
    assert peeked is not None
    assert peeked.access_token == "expiring_access_token"  # still the old one
    assert mock_httpx_token_endpoint["calls"] == []  # no refresh attempted


async def test_get_required_with_no_token_raises_auth_required(
    credentials: Credentials,
) -> None:
    with pytest.raises(AuthRequiredError):
        await credentials.get("example")


async def test_get_optional_with_no_token_returns_none(credentials: Credentials) -> None:
    assert await credentials.get("example", required=False) is None


async def test_clear_removes_stored_credentials(
    credentials: Credentials, fresh_token: TokenInfo
) -> None:
    await credentials.store("example", fresh_token)
    assert await credentials.peek("example") is not None
    await credentials.clear("example")
    assert await credentials.peek("example") is None


async def test_clear_is_idempotent(credentials: Credentials) -> None:
    # Clearing an unset key must not raise.
    await credentials.clear("never_stored")


# ---------------------------------------------------------------------------
# Auto-refresh behavior
# ---------------------------------------------------------------------------


async def test_get_auto_refreshes_near_expiry(
    credentials: Credentials,
    expiring_token: TokenInfo,
    mock_httpx_token_endpoint: dict[str, Any],
) -> None:
    mock_httpx_token_endpoint["response"] = {
        "access_token": "refreshed_access_token",
        "refresh_token": "refreshed_refresh_token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    await credentials.store("example", expiring_token)

    got = await credentials.get("example")
    assert got is not None
    assert got.access_token == "refreshed_access_token"
    assert mock_httpx_token_endpoint["request_data"]["grant_type"] == "refresh_token"
    assert mock_httpx_token_endpoint["request_data"]["refresh_token"] == "valid_refresh_token"

    # Subsequent get reads the refreshed value from storage (no second refresh).
    again = await credentials.get("example")
    assert again is not None
    assert again.access_token == "refreshed_access_token"
    assert len(mock_httpx_token_endpoint["calls"]) == 1


async def test_get_expired_without_refresh_token_raises(
    credentials: Credentials,
) -> None:
    no_refresh = TokenInfo(
        access_token="dead_access",
        refresh_token=None,
        expires_at=time.time() + 30,  # within 5-min buffer
    )
    await credentials.store("example", no_refresh)
    with pytest.raises(AuthRequiredError, match="(?i)re-authentication"):
        await credentials.get("example")


# ---------------------------------------------------------------------------
# Concurrent refresh: exactly one HTTP call when N callers race
# ---------------------------------------------------------------------------


async def test_concurrent_get_triggers_single_refresh(
    credentials: Credentials,
    expiring_token: TokenInfo,
    mock_httpx_token_endpoint: dict[str, Any],
) -> None:
    mock_httpx_token_endpoint["response"] = {
        "access_token": "only_once_token",
        "refresh_token": "rolled_refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    await credentials.store("example", expiring_token)

    results = await asyncio.gather(
        credentials.get("example"),
        credentials.get("example"),
        credentials.get("example"),
        credentials.get("example"),
    )
    assert all(r is not None and r.access_token == "only_once_token" for r in results)
    # Despite 4 concurrent callers, the token endpoint was hit exactly once.
    assert len(mock_httpx_token_endpoint["calls"]) == 1


# ---------------------------------------------------------------------------
# Keyring unavailable → CredentialStorageError
# ---------------------------------------------------------------------------


async def test_no_keyring_raises_credential_storage_error_on_get(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config: OAuthConfig,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> str | None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    creds = Credentials(
        oauth=OAuth(callback_port=8765),
        oauth_configs={"example": oauth_config},
    )
    with pytest.raises(CredentialStorageError, match="No keyring"):
        await creds.get("example", required=False)


async def test_no_keyring_raises_credential_storage_error_on_store(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config: OAuthConfig,
    fresh_token: TokenInfo,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "set_password", boom)
    creds = Credentials(
        oauth=OAuth(callback_port=8765),
        oauth_configs={"example": oauth_config},
    )
    with pytest.raises(CredentialStorageError, match="No keyring"):
        await creds.store("example", fresh_token)


async def test_no_keyring_message_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config: OAuthConfig,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> str | None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    creds = Credentials(
        oauth=OAuth(callback_port=8765),
        oauth_configs={"example": oauth_config},
    )
    try:
        await creds.get("example", required=False)
    except CredentialStorageError as exc:
        msg = str(exc)
        assert "keyrings.alt" in msg or "MCP_CREDENTIALS_STORAGE" in msg
    else:
        pytest.fail("CredentialStorageError not raised")


# ---------------------------------------------------------------------------
# Refresh fails when no oauth config is registered for the api_id
# ---------------------------------------------------------------------------


async def test_refresh_without_registered_config_raises_auth_required(
    mock_keyring: dict[str, str],
    expiring_token: TokenInfo,
) -> None:
    # No oauth_configs registered at all.
    creds = Credentials(oauth=OAuth(callback_port=8765), oauth_configs={})
    await creds.store("orphan", expiring_token)
    with pytest.raises(AuthRequiredError, match="No OAuth configuration"):
        await creds.get("orphan")


# ---------------------------------------------------------------------------
# Secret-omission: stored payload roundtrips but tokens do not appear in logs
# ---------------------------------------------------------------------------


async def test_no_secrets_in_credentials_logs(
    credentials: Credentials,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="mcp.auth")
    secret = TokenInfo(
        access_token="LEAKED_ACCESS_TOKEN",
        refresh_token="LEAKED_REFRESH_TOKEN",
        expires_at=time.time() + 3600,
    )
    await credentials.store("example", secret)
    await credentials.get("example")
    await credentials.clear("example")

    captured = capfd.readouterr()
    full_output = caplog.text + captured.out + captured.err
    for needle in ("LEAKED_ACCESS_TOKEN", "LEAKED_REFRESH_TOKEN"):
        assert needle not in full_output, f"leaked {needle!r} into logs/stdout/stderr"


# ---------------------------------------------------------------------------
# Corrupt keyring payload is treated as missing, not a crash
# ---------------------------------------------------------------------------


async def test_corrupt_stored_payload_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config: OAuthConfig,
) -> None:
    def fake_get(service: str, username: str) -> str | None:
        return "this is not valid json {{"

    monkeypatch.setattr(keyring, "get_password", fake_get)
    creds = Credentials(
        oauth=OAuth(callback_port=8765),
        oauth_configs={"example": oauth_config},
    )
    # Corrupt payload reads as None → required=True still raises AuthRequired.
    with pytest.raises(AuthRequiredError):
        await creds.get("example")
