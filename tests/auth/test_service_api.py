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
    defaults: dict[str, Any] = {
        "type": "session_login",
        "login_path": "/v1/auth",
        "login_method": "POST",
        "credentials": {"api_key": "{api_key}", "secret_key": "{secret_key}"},
        "session_id_field": "data.session_id",
        "session_expire_field": "data.session_expire",
        "user_id_field": "data.user_id",
    }
    defaults.update(auth_overrides)  # overrides win over defaults
    auth = ApiAuthConfig(**defaults)
    return ApiConfig(type="rest", base_url="https://svc.example.com", auth=auth)


class _StubClient:
    def __init__(self, response: httpx.Response, observed: dict[str, Any]) -> None:
        self._response = response
        self._observed = observed

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        data: Any = None,
        headers: Any = None,
    ) -> httpx.Response:
        self._observed["method"] = method
        self._observed["url"] = url
        self._observed["json"] = json
        self._observed["data"] = data
        self._observed["headers"] = dict(headers) if headers else {}
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


# ----------------------------------------------------------------------
# Phase 9.1 — form-encoded login body
# ----------------------------------------------------------------------


async def test_form_urlencoded_login_sends_data_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service APIs like Taximail require form-encoded login bodies, not JSON."""
    response = httpx.Response(
        201,
        json={"data": {"session_id": "abc", "session_expire": 9999999999, "user_id": "u1"}},
    )
    observed = _patch_client(monkeypatch, response)
    cfg = _config(login_content_type="application/x-www-form-urlencoded")

    session = await authenticate(cfg, api_key="K", secret_key="S")

    assert session.session_id == "abc"
    # The body must arrive via `data=` (form-encoded), NOT `json=`.
    assert observed["data"] == {"api_key": "K", "secret_key": "S"}
    assert observed["json"] is None
    assert observed["headers"]["content-type"] == "application/x-www-form-urlencoded"


async def test_json_login_remains_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No login_content_type → JSON path (backward compat)."""
    response = httpx.Response(
        200,
        json={"data": {"session_id": "j", "session_expire": 9999999999, "user_id": "u"}},
    )
    observed = _patch_client(monkeypatch, response)

    await authenticate(_config(), api_key="K", secret_key="S")

    assert observed["json"] == {"api_key": "K", "secret_key": "S"}
    assert observed["data"] is None
    assert observed["headers"]["content-type"] == "application/json"


async def test_unsupported_login_content_type_falls_back_to_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Misconfigured content-type warns on stderr and falls back to JSON."""
    response = httpx.Response(
        200,
        json={"data": {"session_id": "x", "session_expire": 9999999999, "user_id": "u"}},
    )
    observed = _patch_client(monkeypatch, response)
    cfg = _config(login_content_type="application/xml")  # unsupported

    await authenticate(cfg, api_key="K", secret_key="S")

    # Body went via JSON (fallback)
    assert observed["json"] is not None
    assert observed["data"] is None
    captured = capsys.readouterr()
    # Warning to stderr per CLAUDE.md "no stdout for non-protocol" rule
    assert captured.out == ""
    assert "application/xml" in captured.err
    assert "falling back" in captured.err


# ----------------------------------------------------------------------
# Phase 9.1 — `_api_key_fingerprint` user_id derivation
# ----------------------------------------------------------------------


async def test_user_id_fingerprint_namespaces_by_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When user_id_field=_api_key_fingerprint, derive from sha256(api_key)
    namespaced by company_group from the response."""
    response = httpx.Response(
        201,
        json={
            "data": {
                "session_id": "s1",
                "session_expire": 9999999999,
                "company_group": "taximail",
            }
        },
    )
    _patch_client(monkeypatch, response)
    cfg = _config(user_id_field="_api_key_fingerprint")

    session = await authenticate(cfg, api_key="USER-A-KEY", secret_key="S")

    # Expected: "taximail:" + first 16 chars of sha256("USER-A-KEY")
    import hashlib

    expected_fp = hashlib.sha256(b"USER-A-KEY").hexdigest()[:16]
    assert session.user_id == f"taximail:{expected_fp}"


async def test_user_id_fingerprint_deterministic_across_logins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same api_key in two separate logins → same user_id (so the OAuth store
    correctly maps repeat consents back to the same user row)."""
    response = httpx.Response(
        201,
        json={
            "data": {
                "session_id": "s1",
                "session_expire": 9999999999,
                "company_group": "taximail",
            }
        },
    )
    _patch_client(monkeypatch, response)
    cfg = _config(user_id_field="_api_key_fingerprint")

    a = await authenticate(cfg, api_key="SAME-KEY", secret_key="S1")
    b = await authenticate(cfg, api_key="SAME-KEY", secret_key="S2")  # different secret
    assert a.user_id == b.user_id


async def test_user_id_fingerprint_different_keys_distinct_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different api_keys yield two different user_ids — even in the same
    company. Prevents tenant-shared key DB collisions (the original problem)."""
    response = httpx.Response(
        201,
        json={
            "data": {
                "session_id": "s",
                "session_expire": 9999999999,
                "company_group": "taximail",
            }
        },
    )
    _patch_client(monkeypatch, response)
    cfg = _config(user_id_field="_api_key_fingerprint")

    user_a = await authenticate(cfg, api_key="KEY-A", secret_key="X")
    user_b = await authenticate(cfg, api_key="KEY-B", secret_key="X")
    assert user_a.user_id != user_b.user_id
    assert user_a.user_id.startswith("taximail:")
    assert user_b.user_id.startswith("taximail:")


async def test_user_id_fingerprint_falls_back_to_default_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If response has no `company_group`, the prefix falls back to 'default:'."""
    response = httpx.Response(
        200,
        json={"data": {"session_id": "s", "session_expire": 9999999999}},
    )
    _patch_client(monkeypatch, response)
    cfg = _config(user_id_field="_api_key_fingerprint")

    session = await authenticate(cfg, api_key="K", secret_key="S")
    assert session.user_id.startswith("default:")


async def test_fingerprint_does_not_leak_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derived user_id MUST NOT contain the raw api_key (one-way hash)."""
    response = httpx.Response(
        200,
        json={
            "data": {
                "session_id": "s",
                "session_expire": 9999999999,
                "company_group": "taximail",
            }
        },
    )
    _patch_client(monkeypatch, response)
    cfg = _config(user_id_field="_api_key_fingerprint")

    session = await authenticate(cfg, api_key="VERY-SENSITIVE-KEY", secret_key="S")
    assert "VERY-SENSITIVE-KEY" not in session.user_id
