"""Tests for the session_login Service API client.

We avoid spinning up a real HTTP server by patching httpx.AsyncClient's
`request` method with a stub that returns a configurable Response.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from src.auth.credentials import AuthError, AuthRequiredError
from src.auth.service_api import (
    ServiceApiConfigurationError,
    _dotted_lookup,
    authenticate,
)
from src.config import ApiAuthConfig, ApiConfig


def _config(**auth_overrides: Any) -> ApiConfig:
    auth = ApiAuthConfig(
        type="session_login",
        login_path="/v1/auth",
        login_method="POST",
        credentials={"api_key": "{api_key}", "secret_key": "{secret_key}"},
        session_id_field="data.session_id",
        session_expire_field="data.session_expire",
        user_id_field="data.user_id",
        **auth_overrides,
    )
    return ApiConfig(type="rest", base_url="https://svc.example.com", auth=auth)


class _StubClient:
    def __init__(self, response: httpx.Response, observed: dict[str, Any]) -> None:
        self._response = response
        self._observed = observed

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def request(self, method: str, url: str, *, json: Any, headers: Any) -> httpx.Response:
        self._observed["method"] = method
        self._observed["url"] = url
        self._observed["json"] = json
        self._observed["headers"] = dict(headers)
        return self._response


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response | Exception,
) -> dict[str, Any]:
    observed: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> Any:
        if isinstance(response, Exception):
            exc = response

            class _Raises:
                async def __aenter__(self) -> _Raises:
                    return self

                async def __aexit__(self, *_a: Any) -> None:
                    return None

                async def request(self, *_a: Any, **_k: Any) -> httpx.Response:
                    raise exc

            return _Raises()
        return _StubClient(response, observed)

    monkeypatch.setattr("src.auth.service_api.httpx.AsyncClient", factory)
    return observed


async def test_authenticate_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        200,
        json={
            "data": {
                "session_id": "SID",
                "session_expire": 1_900_000_000,
                "user_id": "user-9",
            },
            "company_group": "acme",
        },
    )
    observed = _patch_client(monkeypatch, response)
    session = await authenticate(_config(), api_key="A", secret_key="B")
    assert session.session_id == "SID"
    assert session.user_id == "user-9"
    assert observed["url"] == "https://svc.example.com/v1/auth"
    assert observed["json"] == {"api_key": "A", "secret_key": "B"}


async def test_authenticate_4xx_maps_to_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, httpx.Response(401, json={"error": "bad-creds"}))
    with pytest.raises(AuthRequiredError):
        await authenticate(_config(), api_key="A", secret_key="B")


async def test_authenticate_5xx_maps_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, httpx.Response(503))
    with pytest.raises(AuthError):
        await authenticate(_config(), api_key="A", secret_key="B")


async def test_authenticate_network_failure_maps_to_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, httpx.ConnectError("boom"))
    with pytest.raises(AuthError):
        await authenticate(_config(), api_key="A", secret_key="B")


async def test_authenticate_missing_fields_maps_to_auth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        httpx.Response(200, json={"data": {"session_id": "x"}}),
    )
    with pytest.raises(AuthRequiredError):
        await authenticate(_config(), api_key="A", secret_key="B")


async def test_authenticate_non_session_login_config_raises() -> None:
    bad = ApiConfig(
        type="rest",
        base_url="https://svc.example.com",
        auth=ApiAuthConfig(type="oauth2"),
    )
    with pytest.raises(ServiceApiConfigurationError):
        await authenticate(bad, api_key="A", secret_key="B")


async def test_authenticate_missing_login_path_raises() -> None:
    cfg = _config()
    assert cfg.auth is not None
    cfg.auth.login_path = None  # deliberately invalid post-load
    with pytest.raises(ServiceApiConfigurationError):
        await authenticate(cfg, api_key="A", secret_key="B")


def test_dotted_lookup_returns_none_on_missing_key() -> None:
    assert _dotted_lookup({"a": {"b": 1}}, "a.b.c") is None
    assert _dotted_lookup({}, "x") is None


def test_dotted_lookup_reads_nested() -> None:
    assert _dotted_lookup({"a": {"b": {"c": 42}}}, "a.b.c") == 42


async def test_authenticate_redacts_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even on failure paths, the api_key/secret_key MUST NOT appear in logs."""
    _patch_client(monkeypatch, httpx.Response(401))
    with caplog.at_level("INFO", logger="mcp.auth.service_api"):
        with pytest.raises(AuthRequiredError):
            await authenticate(_config(), api_key="LEAKED-KEY", secret_key="LEAKED-SECRET")
    for record in caplog.records:
        assert "LEAKED-KEY" not in record.getMessage()
        assert "LEAKED-SECRET" not in record.getMessage()
