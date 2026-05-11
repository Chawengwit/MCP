from __future__ import annotations

import pytest
from src.oauth_provider.discovery import (
    DiscoveryError,
    build_authorization_server_metadata,
    build_protected_resource_metadata,
    resolve_issuer,
)


def test_authorization_server_metadata_shape() -> None:
    meta = build_authorization_server_metadata("https://mcp.example.com")
    assert meta["issuer"] == "https://mcp.example.com"
    assert meta["authorization_endpoint"] == "https://mcp.example.com/authorize"
    assert meta["token_endpoint"] == "https://mcp.example.com/token"
    assert meta["registration_endpoint"] == "https://mcp.example.com/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert meta["response_types_supported"] == ["code"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]


def test_protected_resource_metadata_shape() -> None:
    meta = build_protected_resource_metadata("https://mcp.example.com")
    assert meta["resource"] == "https://mcp.example.com/mcp"
    assert meta["authorization_servers"] == ["https://mcp.example.com"]
    assert meta["bearer_methods_supported"] == ["header"]


def test_resolve_issuer_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://mcp.example.com/")
    assert resolve_issuer() == "https://mcp.example.com"  # trailing slash stripped


def test_resolve_issuer_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_OAUTH_ISSUER", raising=False)
    with pytest.raises(DiscoveryError):
        resolve_issuer()


def test_resolve_issuer_rejects_query_or_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://x?foo=bar")
    with pytest.raises(DiscoveryError):
        resolve_issuer()
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://x#frag")
    with pytest.raises(DiscoveryError):
        resolve_issuer()


def test_issuer_explicit_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://env-issuer")
    assert resolve_issuer(explicit="https://explicit-issuer") == "https://explicit-issuer"
