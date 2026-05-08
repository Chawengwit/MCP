"""Tests for scripts/oauth_login.py.

OAuth flow side effects (browser open, callback server, token endpoint POST)
are stubbed via monkeypatch. These tests cover the CLI surface only:
config validation, error paths, exit codes, and the success-path call sequence.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from src.auth import OAuthConfig, TokenInfo
from src.config import (
    ApiAuthConfig,
    ApiConfig,
    ApiLimitsConfig,
    ApiLoggingConfig,
    EndpointConfig,
)

from scripts import oauth_login


def _make_oauth_api_config(**overrides: Any) -> ApiConfig:
    """Build a minimally valid oauth2 ApiConfig for tests."""
    auth_kwargs: dict[str, Any] = {
        "type": "oauth2",
        "provider": "github",
        "client_id": "cid",
        "client_secret": "csecret",
        "authorize_url": "https://example.com/auth",
        "token_url": "https://example.com/token",
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "scopes": ["read:user"],
    }
    auth_kwargs.update(overrides.pop("auth", {}))
    return ApiConfig(
        type="rest",
        base_url="https://api.example.com",
        auth=ApiAuthConfig(**auth_kwargs),
        endpoints={"get_self": EndpointConfig(method="GET", path="/me")},
        logging=ApiLoggingConfig(),
        limits=ApiLimitsConfig(),
    )


@pytest.fixture
def fake_token() -> TokenInfo:
    return TokenInfo(
        access_token="atk",
        refresh_token=None,
        expires_at=time.time() + 3600,
        token_type="Bearer",
        scope="read:user",
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_login_unknown_api_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {})

    rc = await oauth_login.login("github")

    assert rc == 1
    err = capsys.readouterr().err
    assert "API 'github' not found" in err


async def test_login_non_oauth_api_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bearer_api = ApiConfig(
        type="rest",
        base_url="https://api.example.com",
        auth=ApiAuthConfig(type="bearer", token_env="TKN"),
    )
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {"x": bearer_api})

    rc = await oauth_login.login("x")

    assert rc == 1
    assert "is not configured for oauth2" in capsys.readouterr().err


async def test_login_no_auth_block_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    public_api = ApiConfig(type="rest", base_url="https://api.example.com", auth=None)
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {"pub": public_api})

    rc = await oauth_login.login("pub")

    assert rc == 1
    assert "auth.type='none'" in capsys.readouterr().err


async def test_login_missing_required_fields_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_oauth_api_config(auth={"client_id": None, "scopes": []})
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {"github": cfg})

    rc = await oauth_login.login("github")

    assert rc == 1
    err = capsys.readouterr().err
    assert "Missing required oauth2 fields" in err
    assert "client_id" in err
    assert "scopes" in err


async def test_login_oauth_flow_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_oauth_api_config()
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {"github": cfg})

    async def _boom(self: Any, oauth_cfg: OAuthConfig) -> TokenInfo:
        raise RuntimeError("network down")

    monkeypatch.setattr("scripts.oauth_login.OAuth.start_flow", _boom)

    rc = await oauth_login.login("github")

    assert rc == 1
    err = capsys.readouterr().err
    assert "OAuth flow failed: RuntimeError: network down" in err


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_login_success_stores_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_token: TokenInfo,
) -> None:
    cfg = _make_oauth_api_config()
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {"github": cfg})

    captured: dict[str, Any] = {}

    async def _fake_start_flow(self: Any, oauth_cfg: OAuthConfig) -> TokenInfo:
        captured["oauth_cfg"] = oauth_cfg
        return fake_token

    async def _fake_store(self: Any, api_id: str, tokens: TokenInfo) -> None:
        captured["stored_api_id"] = api_id
        captured["stored_token"] = tokens

    monkeypatch.setattr("scripts.oauth_login.OAuth.start_flow", _fake_start_flow)
    monkeypatch.setattr("scripts.oauth_login.Credentials.store", _fake_store)

    rc = await oauth_login.login("github")

    assert rc == 0
    assert captured["stored_api_id"] == "github"
    assert captured["stored_token"] is fake_token
    assert captured["oauth_cfg"].provider == "github"
    assert captured["oauth_cfg"].scopes == ["read:user"]
    out = capsys.readouterr().out
    assert "Authenticated successfully" in out
    assert "service=mcp-data-gateway" in out


async def test_login_clear_first_calls_clear(
    monkeypatch: pytest.MonkeyPatch, fake_token: TokenInfo
) -> None:
    cfg = _make_oauth_api_config()
    monkeypatch.setattr(oauth_login, "load_api_configs", lambda: {"github": cfg})

    calls: list[str] = []

    async def _fake_clear(self: Any, api_id: str) -> None:
        calls.append(f"clear:{api_id}")

    async def _fake_start_flow(self: Any, oauth_cfg: OAuthConfig) -> TokenInfo:
        calls.append("start_flow")
        return fake_token

    async def _fake_store(self: Any, api_id: str, tokens: TokenInfo) -> None:
        calls.append(f"store:{api_id}")

    monkeypatch.setattr("scripts.oauth_login.Credentials.clear", _fake_clear)
    monkeypatch.setattr("scripts.oauth_login.OAuth.start_flow", _fake_start_flow)
    monkeypatch.setattr("scripts.oauth_login.Credentials.store", _fake_store)

    rc = await oauth_login.login("github", clear_first=True)

    assert rc == 0
    assert calls == ["clear:github", "start_flow", "store:github"]


# ---------------------------------------------------------------------------
# Helper: _missing_oauth_fields
# ---------------------------------------------------------------------------


def test_missing_oauth_fields_returns_empty_when_complete() -> None:
    auth = ApiAuthConfig(
        type="oauth2",
        provider="x",
        client_id="cid",
        client_secret="csecret",
        authorize_url="https://example.com/auth",
        token_url="https://example.com/token",
        scopes=["s"],
    )
    assert oauth_login._missing_oauth_fields(auth) == []


def test_missing_oauth_fields_lists_unset_and_empty_values() -> None:
    auth = ApiAuthConfig(
        type="oauth2",
        provider="x",
        client_id=None,  # unset
        client_secret="",  # empty string
        authorize_url="https://example.com/auth",
        token_url="https://example.com/token",
        scopes=[],  # empty list
    )
    missing = oauth_login._missing_oauth_fields(auth)
    assert set(missing) == {"client_id", "client_secret", "scopes"}


# ---------------------------------------------------------------------------
# Helper: _build_oauth_config / MissingOAuthFieldsError
# ---------------------------------------------------------------------------


def test_build_oauth_config_raises_with_missing_attribute() -> None:
    auth = ApiAuthConfig(type="oauth2", provider="x")  # most fields unset
    with pytest.raises(oauth_login.MissingOAuthFieldsError) as exc_info:
        oauth_login._build_oauth_config(auth)
    assert "client_id" in exc_info.value.missing
    assert "scopes" in exc_info.value.missing
    # ValueError is the documented base class
    assert isinstance(exc_info.value, ValueError)


def test_build_oauth_config_returns_oauth_config_when_complete() -> None:
    auth = ApiAuthConfig(
        type="oauth2",
        provider="github",
        client_id="cid",
        client_secret="csecret",
        authorize_url="https://example.com/auth",
        token_url="https://example.com/token",
        redirect_uri="http://127.0.0.1:8765/callback",
        scopes=["read:user"],
    )
    cfg = oauth_login._build_oauth_config(auth)
    assert cfg.provider == "github"
    assert cfg.client_id == "cid"
    assert cfg.scopes == ["read:user"]
    assert cfg.redirect_uri == "http://127.0.0.1:8765/callback"


def test_missing_oauth_fields_error_message_does_not_leak_values() -> None:
    """The error must list only field names, never field values (defensive)."""
    err = oauth_login.MissingOAuthFieldsError(["client_id", "client_secret"])
    rendered = str(err)
    assert "client_id" in rendered
    assert "client_secret" in rendered
    # No accidental value leakage
    assert "csecret" not in rendered


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_parse_args_requires_api_id() -> None:
    with pytest.raises(SystemExit):
        oauth_login._parse_args([])


def test_parse_args_default_clear_false() -> None:
    args = oauth_login._parse_args(["github"])
    assert args.api_id == "github"
    assert args.clear is False


def test_parse_args_clear_flag() -> None:
    args = oauth_login._parse_args(["github", "--clear"])
    assert args.clear is True
