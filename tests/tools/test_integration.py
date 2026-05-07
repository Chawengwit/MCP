"""End-to-end Phase 5 integration: tool call → mocked HTTP → recorded events.

Wires every layer together except the actual network and OAuth flow:
  - Real Recorder writing JSONL to tmp_path
  - Real RestClient + GraphQLClient (with httpx.MockTransport)
  - Real Credentials (in-memory keyring) with a pre-stored TokenInfo
  - Real ToolContext + tool handlers

Asserts the user-visible behavior plus the operator-visible JSONL:
  - 1 audit + 1 usage + 1 insight per call
  - No tokens, secrets, or PII in the captured JSONL
  - Authorization: Bearer <token> reaches the upstream
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.errors
import pytest
from src.auth import Credentials, OAuth, OAuthConfig, TokenInfo
from src.config import ApiAuthConfig, ApiConfig, EndpointConfig
from src.events import Category, JsonlWriter, Recorder, WriterConfig
from src.gateway import GraphQLClient, RestClient
from src.tools import ToolContext, fetch_data_handler


@pytest.fixture
def real_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """In-memory keyring backed by a real dict — the actual Credentials class talks to it."""
    store: dict[str, str] = {}

    def _key(s: str, u: str) -> str:
        return f"{s}:{u}"

    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get(_key(s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__(_key(s, u), p))

    def _delete(s: str, u: str) -> None:
        try:
            del store[_key(s, u)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError("not found") from exc

    monkeypatch.setattr(keyring, "delete_password", _delete)
    return store


@pytest.fixture
def patch_async_client_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Install a custom handler as the AsyncClient transport (shared with gateway tests)."""
    original = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        class _Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr("src.gateway.api_client.httpx.AsyncClient", _Patched)

    return install


def _read_jsonl_dir(category_dir: Path) -> list[dict[str, Any]]:
    if not category_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(category_dir.iterdir()):
        out.extend(json.loads(line) for line in f.read_text().splitlines() if line.strip())
    return out


async def test_full_flow_oauth_api_emits_redacted_event_triple(
    tmp_path: Path,
    real_keyring: dict[str, str],
    patch_async_client_handler: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    # --- Pre-populate keyring with a fresh TokenInfo so the OAuth flow is NOT exercised.
    # Phase 3 unit tests already cover the OAuth flow itself; the integration test
    # asserts the rest of the stack works given a stored credential.
    secret_token = "INTEGRATION_TEST_ACCESS_TOKEN"
    api_id = "demo_rest"

    # --- Mock upstream API: return a small JSON payload, capture the request to assert headers.
    captured: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"users": [{"id": 1, "name": "Ada"}]}',
        )

    patch_async_client_handler(upstream)

    # --- Real Recorder writing to tmp_path; small buffer so events flush quickly.
    log_root = tmp_path / "logs"
    writer = JsonlWriter(
        WriterConfig(
            log_dir=log_root,
            retention_days=1,
            buffer_size=1,
            enabled_categories=frozenset(Category),
        )
    )
    recorder = Recorder(writer)
    await recorder.start()

    try:
        # --- Real Credentials with the pre-populated TokenInfo.
        oauth = OAuth(callback_port=8765)
        oauth_configs = {
            api_id: OAuthConfig(
                provider="demo",
                client_id="cid",
                client_secret="csec",
                authorize_url="https://demo.example.com/oauth/authorize",
                token_url="https://demo.example.com/oauth/token",
                scopes=["read"],
            )
        }
        creds = Credentials(oauth=oauth, oauth_configs=oauth_configs)
        await creds.store(
            api_id,
            TokenInfo(
                access_token=secret_token,
                refresh_token="rt",
                expires_at=time.time() + 3600,
            ),
        )

        # --- Real ApiConfig pointing at the upstream.
        api_config = ApiConfig(
            type="rest",
            base_url="https://demo.example.com",
            auth=ApiAuthConfig(
                type="oauth2",
                provider="demo",
                client_id="cid",
                authorize_url="https://demo.example.com/oauth/authorize",
                token_url="https://demo.example.com/oauth/token",
                scopes=["read"],
            ),
            endpoints={"list_users": EndpointConfig(method="GET", path="/v1/users")},
        )

        # --- Real ToolContext using real RestClient/GraphQLClient.
        ctx = ToolContext(
            configs={api_id: api_config},
            credentials=creds,
            rest_client_factory=lambda c: RestClient(base_url=c.base_url, max_retries=1),
            graphql_client_factory=lambda c: GraphQLClient(url=c.base_url, max_retries=1),
            recorder=recorder,
        )

        # --- Invoke through the same wrapper the server uses.
        result = await fetch_data_handler(
            {"api_id": api_id, "endpoint": "list_users", "filters": {"limit": 5}},
            context=ctx,
        )

        # --- Response shape matches CLAUDE.md.
        assert "data" in result
        assert result["data"] == {"users": [{"id": 1, "name": "Ada"}]}
        assert result["metadata"]["source"] == api_id

        # --- Real Authorization header reached the upstream.
        assert captured, "upstream not hit"
        assert captured[0].headers["authorization"] == f"Bearer {secret_token}"

    finally:
        await recorder.stop()

    # --- JSONL contract: one event per category, no secrets visible.
    audit = _read_jsonl_dir(log_root / "audit")
    usage = _read_jsonl_dir(log_root / "usage")
    insight = _read_jsonl_dir(log_root / "insight")

    fetch_audit = [e for e in audit if e.get("tool") == "fetch_data"]
    fetch_usage = [e for e in usage if e.get("tool") == "fetch_data"]
    fetch_insight = [e for e in insight if e.get("tool") == "fetch_data"]
    assert len(fetch_audit) == 1
    assert fetch_audit[0]["result"] == "success"
    assert len(fetch_usage) == 1
    assert fetch_usage[0]["status"] == "success"
    assert len(fetch_insight) == 1

    # --- Whole-file scan: token never appears anywhere in any JSONL line.
    for category_dir in (log_root / "audit", log_root / "usage", log_root / "insight"):
        for f in category_dir.iterdir():
            blob = f.read_text()
            assert secret_token not in blob, f"access token leaked into {f.name}"
            for needle in ("INTEGRATION_TEST_ACCESS_TOKEN", "Bearer "):
                assert needle not in blob, f"sensitive needle {needle!r} in {f.name}"


async def test_full_flow_unauthenticated_call_emits_error_audit(
    tmp_path: Path,
    real_keyring: dict[str, str],
    patch_async_client_handler: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    """Empty keyring + oauth2 API → AUTH_REQUIRED, audit recorded with result=error,
    and the upstream is never contacted (no OAuth flow either)."""
    api_id = "demo_rest"

    upstream_hits = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_hits
        upstream_hits += 1
        return httpx.Response(status_code=200, content=b"{}")

    patch_async_client_handler(upstream)

    log_root = tmp_path / "logs"
    writer = JsonlWriter(
        WriterConfig(
            log_dir=log_root,
            retention_days=1,
            buffer_size=1,
            enabled_categories=frozenset(Category),
        )
    )
    recorder = Recorder(writer)
    await recorder.start()

    try:
        oauth = OAuth(callback_port=8765)
        creds = Credentials(
            oauth=oauth,
            oauth_configs={
                api_id: OAuthConfig(
                    provider="demo",
                    client_id="cid",
                    client_secret="csec",
                    authorize_url="https://demo.example.com/oauth/authorize",
                    token_url="https://demo.example.com/oauth/token",
                    scopes=["read"],
                )
            },
        )
        # Note: NO creds.store() — keyring is empty.

        api_config = ApiConfig(
            type="rest",
            base_url="https://demo.example.com",
            auth=ApiAuthConfig(
                type="oauth2",
                provider="demo",
                client_id="cid",
                authorize_url="https://demo.example.com/oauth/authorize",
                token_url="https://demo.example.com/oauth/token",
                scopes=["read"],
            ),
            endpoints={"list_users": EndpointConfig(method="GET", path="/v1/users")},
        )

        ctx = ToolContext(
            configs={api_id: api_config},
            credentials=creds,
            rest_client_factory=lambda c: RestClient(base_url=c.base_url, max_retries=1),
            graphql_client_factory=lambda c: GraphQLClient(url=c.base_url, max_retries=1),
            recorder=recorder,
        )

        result = await fetch_data_handler({"api_id": api_id, "endpoint": "list_users"}, context=ctx)
        assert result["error"]["code"] == "AUTH_REQUIRED"
        # The upstream API was never hit — no token was sent in the clear.
        assert upstream_hits == 0
    finally:
        await recorder.stop()

    audit = _read_jsonl_dir(log_root / "audit")
    fetch_audit = [e for e in audit if e.get("tool") == "fetch_data"]
    assert len(fetch_audit) == 1
    assert fetch_audit[0]["result"] == "error"
