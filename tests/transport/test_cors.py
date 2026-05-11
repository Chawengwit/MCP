"""Tests for the CORS middleware (Phase 9.3).

Covers the cases the browser actually depends on: preflight, allow-origin
echo, expose-headers, pass-through for non-CORS paths, and the wildcard
``"*"`` mode. The middleware is small enough that one targeted test per
behaviour is enough — no need for end-to-end ASGI server tests here.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import pytest
from src.transport.cors import (
    DEFAULT_ALLOWED_ORIGINS,
    cors_middleware,
    resolve_allowed_origins,
)


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
        lower = name.lower()
        for n, v in self.headers:
            if n.lower() == lower:
                return v
        return None


def _scope(*, path: str, method: str = "GET", origin: str | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    return {"type": "http", "method": method, "path": path, "headers": headers}


async def _empty_receive() -> MutableMapping[str, Any]:
    return {"type": "http.disconnect"}


class _Inner:
    """Stub ASGI app — records that it was called, optionally sends a
    minimal 200 response so we can verify the CORS wrapper injected
    headers correctly."""

    def __init__(self, *, respond: bool = True) -> None:
        self.calls = 0
        self.respond = respond

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        if not self.respond:
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})


# ----------------------------------------------------------------------
# resolve_allowed_origins
# ----------------------------------------------------------------------


def test_resolve_allowed_origins_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_CORS_ALLOWED_ORIGINS", raising=False)
    assert resolve_allowed_origins() == DEFAULT_ALLOWED_ORIGINS


def test_resolve_allowed_origins_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CORS_ALLOWED_ORIGINS", "*")
    assert resolve_allowed_origins() == ("*",)


def test_resolve_allowed_origins_csv_strips_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MCP_CORS_ALLOWED_ORIGINS",
        "https://claude.ai , https://claude.ai , https://www.claude.ai",
    )
    assert resolve_allowed_origins() == ("https://claude.ai", "https://www.claude.ai")


def test_resolve_allowed_origins_blank_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CORS_ALLOWED_ORIGINS", "  ")
    assert resolve_allowed_origins() == DEFAULT_ALLOWED_ORIGINS


# ----------------------------------------------------------------------
# Pass-through for non-CORS scopes
# ----------------------------------------------------------------------


async def test_non_http_scope_passes_through() -> None:
    inner = _Inner()
    mw = cors_middleware(inner, allowed_origins=["http://x"])
    await mw({"type": "lifespan", "headers": []}, _empty_receive, _Capture().send)
    assert inner.calls == 1


async def test_non_cors_path_passes_through_without_cors_headers() -> None:
    """``/authorize`` is browser-navigation, not fetch — no CORS needed."""
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://allowed"])
    await mw(_scope(path="/authorize", origin="http://allowed"), _empty_receive, cap.send)
    assert inner.calls == 1
    assert cap.header(b"access-control-allow-origin") is None


# ----------------------------------------------------------------------
# OPTIONS preflight
# ----------------------------------------------------------------------


async def test_preflight_with_allowed_origin_returns_204_with_headers() -> None:
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://localhost:6274"])
    await mw(
        _scope(path="/mcp", method="OPTIONS", origin="http://localhost:6274"),
        _empty_receive,
        cap.send,
    )
    assert cap.status == 204
    # Inner app is not invoked — preflight is a CORS concept.
    assert inner.calls == 0
    assert cap.header(b"access-control-allow-origin") == b"http://localhost:6274"
    assert b"POST" in (cap.header(b"access-control-allow-methods") or b"")
    assert b"Authorization" in (cap.header(b"access-control-allow-headers") or b"")
    assert cap.header(b"vary") == b"Origin"
    assert cap.header(b"access-control-max-age") == b"86400"


async def test_preflight_with_unallowed_origin_returns_204_without_cors_headers() -> None:
    """Preflight from a disallowed origin still 204s (browser will then refuse
    the real request) — we must NOT emit allow-origin for an unknown origin."""
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://localhost:6274"])
    await mw(
        _scope(path="/mcp", method="OPTIONS", origin="https://attacker.example"),
        _empty_receive,
        cap.send,
    )
    assert cap.status == 204
    assert cap.header(b"access-control-allow-origin") is None


async def test_preflight_on_well_known_endpoint() -> None:
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://localhost:6274"])
    await mw(
        _scope(
            path="/.well-known/oauth-authorization-server",
            method="OPTIONS",
            origin="http://localhost:6274",
        ),
        _empty_receive,
        cap.send,
    )
    assert cap.status == 204
    assert cap.header(b"access-control-allow-origin") == b"http://localhost:6274"


# ----------------------------------------------------------------------
# Actual requests on CORS-eligible paths
# ----------------------------------------------------------------------


async def test_allowed_origin_response_carries_cors_headers() -> None:
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://localhost:6274"])
    await mw(
        _scope(path="/token", method="POST", origin="http://localhost:6274"),
        _empty_receive,
        cap.send,
    )
    assert inner.calls == 1
    assert cap.status == 200
    assert cap.header(b"access-control-allow-origin") == b"http://localhost:6274"
    assert cap.header(b"vary") == b"Origin"
    assert b"Mcp-Session-Id" in (cap.header(b"access-control-expose-headers") or b"")
    assert b"WWW-Authenticate" in (cap.header(b"access-control-expose-headers") or b"")
    assert cap.header(b"access-control-allow-credentials") == b"true"


async def test_unallowed_origin_response_has_no_cors_headers() -> None:
    """Non-allowed cross-origin response goes through unchanged. The browser
    will block the read; non-browser clients (curl, server-to-server, the
    Phase 9.2 smoke test) keep working unaffected."""
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://localhost:6274"])
    await mw(
        _scope(path="/token", method="POST", origin="https://attacker.example"),
        _empty_receive,
        cap.send,
    )
    assert inner.calls == 1
    assert cap.header(b"access-control-allow-origin") is None


async def test_no_origin_header_passes_through_to_inner_app() -> None:
    """Server-to-server callers (the smoke test, curl) don't send Origin —
    must not be blocked and must not gain CORS headers."""
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["http://localhost:6274"])
    await mw(_scope(path="/token", method="POST"), _empty_receive, cap.send)
    assert inner.calls == 1
    assert cap.header(b"access-control-allow-origin") is None


async def test_wildcard_mode_allows_any_origin_echoing_request_origin() -> None:
    inner = _Inner()
    cap = _Capture()
    mw = cors_middleware(inner, allowed_origins=["*"])
    await mw(
        _scope(path="/mcp", method="POST", origin="https://anyclient.example"),
        _empty_receive,
        cap.send,
    )
    assert inner.calls == 1
    # Even in wildcard mode we echo the origin — required for credentialed
    # requests, and avoids leaking the wildcard policy.
    assert cap.header(b"access-control-allow-origin") == b"https://anyclient.example"
