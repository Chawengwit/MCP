"""Tests for the HTTP transport (`src.transport.http`).

Coverage:
  - `check_loopback_guard` — refuses non-loopback bind without token
  - `bearer_auth_middleware` — 401 on missing / wrong token, pass-through on match
  - `build_app` end-to-end — initialize + tools/list round-trip via Starlette TestClient
  - secret-omission — bearer token literal does not leak into response bodies

Strategy: build a real `Server` from `_build_server` with a no-op registry, mount it via
`build_app`, and exercise it through `starlette.testclient.TestClient` (which handles
lifespan setup so `StreamableHTTPSessionManager.run()` enters/exits cleanly).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from mcp import types
from mcp.server import Server
from src.transport import http as http_module
from src.transport.http import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LOOPBACK_HOSTS,
    LoopbackGuardError,
    bearer_auth_middleware,
    build_app,
    check_loopback_guard,
    resolve_http_settings,
    run_http,
)
from starlette.testclient import TestClient

VALID_TOKEN = "secret-test-token-do-not-leak"
WRONG_TOKEN = "wrong"


def _make_minimal_server() -> Server:
    """Build a tiny MCP Server that exposes one no-op tool.

    Mirrors the shape of `src.server._build_server` but stays decoupled from the
    full ToolContext so tests don't need to wire up Recorder / Credentials.
    """
    server: Server = Server("mcp-data-gateway-test")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="ping",
                description="Test-only no-op tool.",
                inputSchema={"type": "object", "properties": {}, "required": []},
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"pong": True}))],
            isError=False,
        )

    return server


# ---------------------------------------------------------------------------
# check_loopback_guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
def test_loopback_bind_without_token_is_allowed(host: str) -> None:
    # No raise = pass.
    check_loopback_guard(host, "")


def test_public_bind_with_token_is_allowed() -> None:
    check_loopback_guard("0.0.0.0", "any-token")


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::"])
def test_public_bind_without_token_is_refused(host: str) -> None:
    with pytest.raises(LoopbackGuardError) as exc_info:
        check_loopback_guard(host, "")
    msg = str(exc_info.value)
    assert host in msg
    assert "MCP_HTTP_BEARER_TOKEN" in msg


def test_loopback_guard_message_does_not_echo_token_value() -> None:
    """The guard message must never contain a token (defensive — even on the empty path)."""
    with pytest.raises(LoopbackGuardError) as exc_info:
        check_loopback_guard("0.0.0.0", "")
    assert VALID_TOKEN not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Phase 9: loopback guard relaxation when OAuth Provider is on
# ---------------------------------------------------------------------------


def test_loopback_guard_loopback_no_token_oauth_off_is_ok() -> None:
    """(a) loopback + no token + OAuth off  → OK"""
    check_loopback_guard("127.0.0.1", "", oauth_enabled=False)


def test_loopback_guard_public_no_token_oauth_off_raises() -> None:
    """(b) public + no token + OAuth off    → LoopbackGuardError"""
    with pytest.raises(LoopbackGuardError):
        check_loopback_guard("0.0.0.0", "", oauth_enabled=False)


def test_loopback_guard_public_no_token_oauth_on_is_ok() -> None:
    """(c) public + no token + OAuth on     → OK (per-user OAuth tokens authenticate)"""
    check_loopback_guard("0.0.0.0", "", oauth_enabled=True)


# ---------------------------------------------------------------------------
# bearer_auth_middleware (unit tests via TestClient + a minimal app)
# ---------------------------------------------------------------------------


def _build_app_for_auth_tests(token: str) -> Any:
    """Build a Starlette app that just echoes 'ok' under the Bearer middleware."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def echo(_request: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/anything", endpoint=echo, methods=["POST"])])
    return bearer_auth_middleware(inner, token)


def test_missing_authorization_header_returns_401() -> None:
    app = _build_app_for_auth_tests(VALID_TOKEN)
    with TestClient(app) as client:
        r = client.post("/anything")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_non_bearer_scheme_returns_401() -> None:
    app = _build_app_for_auth_tests(VALID_TOKEN)
    with TestClient(app) as client:
        r = client.post("/anything", headers={"Authorization": f"Basic {VALID_TOKEN}"})
    assert r.status_code == 401


def test_wrong_token_returns_401() -> None:
    app = _build_app_for_auth_tests(VALID_TOKEN)
    with TestClient(app) as client:
        r = client.post("/anything", headers={"Authorization": f"Bearer {WRONG_TOKEN}"})
    assert r.status_code == 401


def test_token_with_wrong_length_returns_401_without_constant_time_call() -> None:
    """secrets.compare_digest requires equal-length operands; a length-mismatch must
    short-circuit cleanly (not raise) and return 401."""
    app = _build_app_for_auth_tests(VALID_TOKEN)
    with TestClient(app) as client:
        r = client.post("/anything", headers={"Authorization": "Bearer x"})
    assert r.status_code == 401


def test_correct_token_passes_through() -> None:
    app = _build_app_for_auth_tests(VALID_TOKEN)
    with TestClient(app) as client:
        r = client.post("/anything", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_empty_expected_token_disables_auth() -> None:
    """Loopback dev mode: empty token means the middleware is a no-op."""
    app = _build_app_for_auth_tests("")
    with TestClient(app) as client:
        r = client.post("/anything")  # no auth header at all
    assert r.status_code == 200


def test_unauthorized_response_does_not_echo_supplied_token() -> None:
    """A defensive check: the 401 body must never contain the user-supplied token."""
    app = _build_app_for_auth_tests(VALID_TOKEN)
    leaky = "leak-me-please-12345"
    with TestClient(app) as client:
        r = client.post("/anything", headers={"Authorization": f"Bearer {leaky}"})
    assert r.status_code == 401
    assert leaky not in r.text


# ---------------------------------------------------------------------------
# build_app — full Streamable HTTP round-trip
# ---------------------------------------------------------------------------


def _initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "pytest-http-transport", "version": "0.0.1"},
        },
    }


def _tools_list_payload(req_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}}


def test_build_app_initialize_then_tools_list_round_trip() -> None:
    server = _make_minimal_server()
    app = build_app(server, expected_token=VALID_TOKEN, json_response=True)
    auth = {"Authorization": f"Bearer {VALID_TOKEN}"}
    accept = {"Accept": "application/json, text/event-stream"}

    with TestClient(app) as client:
        init = client.post("/mcp", json=_initialize_payload(), headers={**auth, **accept})
        assert init.status_code == 200, init.text
        session_id = init.headers.get("mcp-session-id")
        assert session_id, f"server did not return Mcp-Session-Id header: {init.headers}"

        # Spec requires `notifications/initialized` after the initialize response.
        notify = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**auth, **accept, "mcp-session-id": session_id},
        )
        assert notify.status_code in (200, 202), notify.text

        tools = client.post(
            "/mcp",
            json=_tools_list_payload(2),
            headers={**auth, **accept, "mcp-session-id": session_id},
        )
        assert tools.status_code == 200, tools.text
        body = tools.json()
        assert body["id"] == 2
        names = [t["name"] for t in body["result"]["tools"]]
        assert "ping" in names


def test_build_app_initialize_without_bearer_token_returns_401() -> None:
    server = _make_minimal_server()
    app = build_app(server, expected_token=VALID_TOKEN, json_response=True)
    with TestClient(app) as client:
        r = client.post(
            "/mcp",
            json=_initialize_payload(),
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 401


def test_build_app_unknown_path_returns_404_not_307() -> None:
    """`/wrong` must 404; we don't want the Mount-style redirect on unknown paths."""
    server = _make_minimal_server()
    app = build_app(server, expected_token=VALID_TOKEN, json_response=True)
    with TestClient(app) as client:
        r = client.post(
            "/wrong",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}


def test_build_app_mcp_path_does_not_redirect_to_trailing_slash() -> None:
    """Earlier prototype used Starlette Mount, which 307-redirected /mcp → /mcp/.
    The current dispatcher serves /mcp directly. Catch a regression here."""
    server = _make_minimal_server()
    app = build_app(server, expected_token=VALID_TOKEN, json_response=True)
    auth = {"Authorization": f"Bearer {VALID_TOKEN}"}
    accept = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, follow_redirects=False) as client:
        r = client.post("/mcp", json=_initialize_payload(), headers={**auth, **accept})
    assert r.status_code != 307


# ---------------------------------------------------------------------------
# resolve_http_settings — env-var validation
# ---------------------------------------------------------------------------


def test_resolve_http_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_HTTP_HOST", raising=False)
    monkeypatch.delenv("MCP_HTTP_PORT", raising=False)
    monkeypatch.delenv("MCP_HTTP_BEARER_TOKEN", raising=False)
    host, port, token = resolve_http_settings()
    assert host == DEFAULT_HOST
    assert port == DEFAULT_PORT
    assert token == ""


def test_resolve_http_settings_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HTTP_HOST", "  127.0.0.1  ")
    monkeypatch.setenv("MCP_HTTP_PORT", "  9090  ")
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "  tok  ")
    host, port, token = resolve_http_settings()
    assert (host, port, token) == ("127.0.0.1", 9090, "tok")


def test_resolve_http_settings_rejects_non_integer_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_HTTP_PORT", "eighty-eighty")
    with pytest.raises(ValueError, match="must be an integer"):
        resolve_http_settings()


@pytest.mark.parametrize("port", ["0", "-1", "65536", "70000"])
def test_resolve_http_settings_rejects_out_of_range_port(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv("MCP_HTTP_PORT", port)
    with pytest.raises(ValueError, match="range 1..65535"):
        resolve_http_settings()


# ---------------------------------------------------------------------------
# run_http — per-arg env fallback contract
#
# The four tests below share a small harness so each can observe the resolved
# host / port / token end-to-end. The key assertion is on the token, which is
# consumed by `build_app` and never reaches `uvicorn.Config` — without a
# build_app spy, a regression that substituted a different token would slip
# through.
# ---------------------------------------------------------------------------


def _install_run_http_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `resolve_http_settings`, `build_app`, and `uvicorn.Server.serve`.

    Returns a dict of captured values:
      env_calls    — count of resolve_http_settings invocations
      seen_tokens  — every token value passed to build_app (one entry per call)
      serve_host   — host passed to uvicorn.Config (None until serve runs)
      serve_port   — port passed to uvicorn.Config

    Tests can assert on any subset of these to verify the per-arg overlay
    contract without spinning up real uvicorn / Starlette / SDK lifecycle.
    """
    captured: dict[str, Any] = {
        "env_calls": 0,
        "seen_tokens": [],
        "serve_host": None,
        "serve_port": None,
    }

    def _resolve_spy() -> tuple[str, int, str]:
        captured["env_calls"] += 1
        return ("env-host", 9999, "env-token")

    async def _noop_app(scope: Any, receive: Any, send: Any) -> None:
        pass  # never invoked because uvicorn.Server.serve is mocked

    def _build_app_spy(
        server: Any,
        token: str,
        *,
        json_response: bool = False,
        oauth_dispatcher: Any = None,
        oauth_middleware: Any = None,
    ) -> Any:
        captured["seen_tokens"].append(token)
        return _noop_app

    async def _fake_serve(self: Any) -> None:
        captured["serve_host"] = self.config.host
        captured["serve_port"] = self.config.port

    monkeypatch.setattr(http_module, "resolve_http_settings", _resolve_spy)
    monkeypatch.setattr(http_module, "build_app", _build_app_spy)
    monkeypatch.setattr("uvicorn.Server.serve", _fake_serve)
    return captured


async def test_run_http_explicit_args_skip_env_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three kwargs explicit → env not read; explicit values reach build_app + uvicorn."""
    captured = _install_run_http_spies(monkeypatch)
    server = _make_minimal_server()

    await run_http(server, host="127.0.0.1", port=12345, token="explicit-token")

    assert captured["env_calls"] == 0, (
        f"resolve_http_settings should not have been called when all args are "
        f"explicit; got {captured['env_calls']} call(s)"
    )
    assert captured["seen_tokens"] == ["explicit-token"]
    assert captured["serve_host"] == "127.0.0.1"
    assert captured["serve_port"] == 12345


async def test_run_http_no_args_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default invocation reads all three slots from env."""
    captured = _install_run_http_spies(monkeypatch)
    # Force the env-host into the loopback set so check_loopback_guard passes.
    monkeypatch.setattr(http_module, "LOOPBACK_HOSTS", frozenset({"env-host"}))
    server = _make_minimal_server()

    await run_http(server)

    assert captured["env_calls"] == 1
    assert captured["seen_tokens"] == ["env-token"]
    assert captured["serve_host"] == "env-host"
    assert captured["serve_port"] == 9999


async def test_run_http_partial_args_overlay_env_for_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwargs win; missing slots come from env, including the token slot.

    The build_app spy (`seen_tokens`) is the assertion that proves the env-token
    was actually substituted in — the token never reaches uvicorn.Config, so
    capturing it requires intercepting build_app.
    """
    captured = _install_run_http_spies(monkeypatch)
    server = _make_minimal_server()

    # Override host + port; token (None) must be filled from env.
    await run_http(server, host="127.0.0.1", port=12345)

    assert captured["env_calls"] == 1, "lazy single env read"
    assert captured["serve_host"] == "127.0.0.1", "explicit host wins"
    assert captured["serve_port"] == 12345, "explicit port wins"
    assert captured["seen_tokens"] == ["env-token"], (
        f"missing token kwarg must overlay env-token; build_app saw {captured['seen_tokens']!r}"
    )


async def test_run_http_explicit_token_overrides_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit `token=""` is honored as-is; env token is NOT substituted in.

    Empty string is a valid explicit token value (means "no auth", reachable
    only on loopback per the guard). Only `None` triggers env fallback for
    that slot. This test pins the contract.
    """
    captured = _install_run_http_spies(monkeypatch)
    server = _make_minimal_server()

    await run_http(server, host="127.0.0.1", port=55555, token="")

    # All three explicit → no env read at all, even though env_token would
    # have been "env-token" via the spy.
    assert captured["env_calls"] == 0
    assert captured["seen_tokens"] == [""], (
        f"explicit token='' must override env; build_app saw {captured['seen_tokens']!r}"
    )
    assert captured["serve_host"] == "127.0.0.1"
    assert captured["serve_port"] == 55555


# ---------------------------------------------------------------------------
# build_app — empty-token defensive warning
# ---------------------------------------------------------------------------


def test_build_app_with_empty_token_warns_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _make_minimal_server()
    build_app(server, expected_token="")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "no bearer token" in err
    assert "loopback" in err.lower()


def test_build_app_with_token_does_not_emit_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _make_minimal_server()
    build_app(server, expected_token=VALID_TOKEN)
    err = capsys.readouterr().err
    assert "WARNING" not in err


def test_build_app_response_does_not_leak_bearer_token() -> None:
    """End-to-end secret-omission canary: after a real round-trip the Bearer token
    literal must not appear anywhere in any response body."""
    server = _make_minimal_server()
    app = build_app(server, expected_token=VALID_TOKEN, json_response=True)
    auth = {"Authorization": f"Bearer {VALID_TOKEN}"}
    accept = {"Accept": "application/json, text/event-stream"}

    seen_bodies: list[str] = []
    with TestClient(app) as client:
        init = client.post("/mcp", json=_initialize_payload(), headers={**auth, **accept})
        seen_bodies.append(init.text)
        session_id = init.headers.get("mcp-session-id", "")
        if session_id:
            tools = client.post(
                "/mcp",
                json=_tools_list_payload(uuid4().int & 0x7FFFFFFF),
                headers={**auth, **accept, "mcp-session-id": session_id},
            )
            seen_bodies.append(tools.text)

    for body in seen_bodies:
        assert VALID_TOKEN not in body, "bearer token leaked into response body"
