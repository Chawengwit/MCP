"""End-to-end OAuth Provider smoke test for local development.

Drives the full Phase 9 OAuth dance against a locally-running MCP server
WITHOUT relying on MCP Inspector (which has a flaky token-exchange step in
some versions). The flow:

  1. Discovery (RFC 8414)                 → /.well-known/oauth-authorization-server
  2. Dynamic Client Registration (7591)   → POST /register
  3. PKCE generation (RFC 7636 S256)
  4. POST /authorize/consent              → Taximail login + auth_code (302)
  5. POST /token                          → access_token + refresh_token
  6. POST /mcp initialize                 → mcp-session-id
  7. POST /mcp tools/list
  8. POST /mcp tools/call list_apis
  9. POST /mcp tools/call fetch_data on taximail (the real test!)

Usage:
    .venv/bin/python -m scripts.oauth_smoke_test

Credentials are read from env vars (preferred) or prompted on stdin:
    TAXIMAIL_API_KEY=...
    TAXIMAIL_SECRET_KEY=...

The server URL defaults to ``http://127.0.0.1:8080`` (the MCP server's
loopback bind). Override via ``MCP_BASE_URL``.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

BASE = os.environ.get("MCP_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
LOCAL_CALLBACK = "http://127.0.0.1:9090/callback"  # never actually listened on


def _step(n: int, title: str) -> None:
    print(f"\n\033[1;36m[{n}] {title}\033[0m")


def _ok(msg: str) -> None:
    print(f"    \033[1;32m✓\033[0m {msg}")


def _fail(msg: str) -> None:
    print(f"    \033[1;31m✗\033[0m {msg}")


def _read_credentials() -> tuple[str, str]:
    api_key = os.environ.get("TAXIMAIL_API_KEY", "").strip()
    secret_key = os.environ.get("TAXIMAIL_SECRET_KEY", "").strip()
    if not api_key:
        api_key = input("Service API api_key: ").strip()
    if not secret_key:
        secret_key = getpass.getpass("Service API secret_key (hidden): ").strip()
    if not api_key or not secret_key:
        print("api_key and secret_key are required.", file=sys.stderr)
        sys.exit(2)
    return api_key, secret_key


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Extract JSON-RPC objects from an SSE-encoded body."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
    return out


def main() -> int:
    api_key, secret_key = _read_credentials()

    # 1. Discovery
    _step(1, "Discovery (RFC 8414)")
    meta = httpx.get(f"{BASE}/.well-known/oauth-authorization-server").json()
    _ok(f"issuer = {meta['issuer']}")
    _ok(f"endpoints: authorize={meta['authorization_endpoint']}")
    _ok(f"endpoints: token={meta['token_endpoint']}")

    # 2. Dynamic Client Registration
    _step(2, "Dynamic Client Registration (RFC 7591)")
    reg = httpx.post(
        meta["registration_endpoint"],
        json={"client_name": "Smoke Test", "redirect_uris": [LOCAL_CALLBACK]},
    ).json()
    client_id = reg["client_id"]
    _ok(f"client_id = {client_id}")

    # 3. PKCE
    _step(3, "Generate PKCE (S256)")
    verifier = secrets.token_urlsafe(64)[:64]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    _ok(f"verifier length = {len(verifier)} chars")

    # 4. POST /authorize/consent — bypass the HTML form, hit the handler directly
    _step(4, "POST /authorize/consent (programmatic)")
    consent = httpx.post(
        f"{BASE}/authorize/consent",
        data={
            "api_key": api_key,
            "secret_key": secret_key,
            "client_id": client_id,
            "redirect_uri": LOCAL_CALLBACK,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_type": "code",
        },
        follow_redirects=False,
    )
    if consent.status_code != 302:
        _fail(f"expected 302, got {consent.status_code}: {consent.text[:300]}")
        return 1
    location = consent.headers["location"]
    qs = parse_qs(urlparse(location).query)
    code = qs.get("code", [""])[0]
    if not code:
        _fail(f"no code in redirect: {location}")
        return 1
    _ok(f"302 → {LOCAL_CALLBACK}?code={code[:20]}...&state=...")

    # 5. Exchange code for access_token
    _step(5, "POST /token (exchange code → access_token)")
    tok = httpx.post(
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": LOCAL_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if tok.status_code != 200:
        _fail(f"status={tok.status_code}: {tok.text}")
        return 1
    token_data = tok.json()
    access_token = token_data["access_token"]
    _ok(f"access_token = {access_token[:24]}... (len={len(access_token)})")
    _ok(f"refresh_token present: {bool(token_data.get('refresh_token'))}")
    _ok(f"expires_in = {token_data['expires_in']}s")

    # 6. /mcp initialize
    _step(6, "POST /mcp initialize")
    mcp_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = httpx.post(
        f"{BASE}/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1"},
            },
            "id": 1,
        },
    )
    if init.status_code != 200:
        _fail(f"status={init.status_code}: {init.text[:300]}")
        return 1
    session_id = init.headers.get("mcp-session-id", "")
    _ok(f"mcp-session-id = {session_id}")

    # 7. tools/list
    _step(7, "POST /mcp tools/list")
    mcp_headers["mcp-session-id"] = session_id
    # Some MCP servers require an initialized notification before tools/list
    httpx.post(
        f"{BASE}/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    lst = httpx.post(
        f"{BASE}/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2},
    )
    tools_seen: list[str] = []
    for obj in _parse_sse(lst.text):
        result = obj.get("result")
        if isinstance(result, dict) and "tools" in result:
            tools_seen = [t["name"] for t in result["tools"]]
    if not tools_seen:
        _fail(f"could not parse tools from body: {lst.text[:400]}")
        return 1
    _ok(f"tools = {tools_seen}")

    # 8. tools/call list_apis
    _step(8, "POST /mcp tools/call list_apis")
    la = httpx.post(
        f"{BASE}/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "list_apis", "arguments": {}},
            "id": 3,
        },
    )
    for obj in _parse_sse(la.text):
        result = obj.get("result")
        if isinstance(result, dict):
            content = result.get("content", [])
            for item in content:
                if item.get("type") == "text":
                    parsed = json.loads(item["text"])
                    data = parsed.get("data")
                    # `list_apis` can return data as a list of api_ids (strings),
                    # a list of dicts with `api_id` key, or a dict keyed by id —
                    # all are legal MCP-tool response shapes. Handle each.
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        apis = [a.get("api_id", a.get("id", "?")) for a in data]
                    elif isinstance(data, list):
                        apis = [str(a) for a in data]
                    elif isinstance(data, dict):
                        apis = list(data.keys())
                    else:
                        apis = [f"<unparseable: {type(data).__name__}>"]
                    _ok(f"available APIs = {apis}")

    # 9. tools/call fetch_data — taximail list_subscribers (THE REAL TEST!)
    _step(9, "POST /mcp tools/call fetch_data (taximail/list_subscribers)")
    fd = httpx.post(
        f"{BASE}/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "fetch_data",
                "arguments": {
                    "api_id": "taximail",
                    "endpoint": "list_subscribers",
                    "filters": {
                        "display_mode": "all",
                        "order_by": "email",
                        "order_type": "desc",
                        "page": 1,
                        "limit": 3,
                    },
                },
            },
            "id": 4,
        },
    )
    print(f"    HTTP status: {fd.status_code}")
    found_data = False
    parsed_any = False
    for obj in _parse_sse(fd.text):
        parsed_any = True
        # Surface JSON-RPC level errors first (auth / validation / etc.)
        if "error" in obj:
            _fail(f"JSON-RPC error: {json.dumps(obj['error'], ensure_ascii=False)}")
            return 1
        result = obj.get("result")
        if not isinstance(result, dict):
            continue
        # If the tool errored, MCP wraps it as `isError: true` with the error
        # message inside the text content. Surface it raw — easier to debug.
        if result.get("isError"):
            for item in result.get("content", []):
                if item.get("type") == "text":
                    _fail(f"tool reported isError=True:\n{item.get('text', '')[:1000]}")
            return 1
        for item in result.get("content", []):
            if item.get("type") != "text":
                continue
            raw_text = item.get("text", "")
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                # Not JSON — print raw and continue. Useful when the tool
                # surfaces a plain-text error string.
                print("    (response text is not JSON — printing raw:)")
                print(raw_text[:2000])
                found_data = True
                continue
            if isinstance(parsed, dict) and "error" in parsed:
                _fail(f"tool error envelope: {json.dumps(parsed['error'], ensure_ascii=False)}")
                return 1
            _ok("✨ Taximail responded — full Phase 9 flow works!")
            print()
            print(json.dumps(parsed, indent=2, ensure_ascii=False)[:2000])
            found_data = True
    if not parsed_any:
        _fail(f"no SSE events parsed: {fd.text[:600]}")
        return 1
    if not found_data:
        _fail(f"no content found in tool result: {fd.text[:600]}")
        return 1

    print()
    print("\033[1;32m" + "=" * 60 + "\033[0m")
    print("\033[1;32m✅ FULL OAUTH FLOW SUCCESS — Phase 9 end-to-end verified\033[0m")
    print("\033[1;32m" + "=" * 60 + "\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
