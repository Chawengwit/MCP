from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from src.auth import AuthRequiredError, OAuth, OAuthConfig, TokenInfo
from src.config import ApiAuthConfig, ApiConfig, ApiLimitsConfig, EndpointConfig
from src.events import Category, JsonlWriter, Recorder, WriterConfig
from src.gateway import GraphQLClient, RestClient
from src.tools import ToolContext
from src.tools.mcp_tools import (
    FetchDataInput,
    GetStatusInput,
    GraphQLInput,
    SendDataInput,
    execute_graphql,
    execute_graphql_handler,
    fetch_data,
    fetch_data_handler,
    get_status,
    send_data,
    send_data_handler,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rest_api_config() -> ApiConfig:
    return ApiConfig(
        type="rest",
        base_url="https://api.example.com",
        auth=ApiAuthConfig(
            type="oauth2",
            provider="example",
            client_id="cid",
            authorize_url="https://api.example.com/oauth/authorize",
            token_url="https://api.example.com/oauth/token",
            scopes=["read"],
        ),
        endpoints={
            "list_users": EndpointConfig(method="GET", path="/v1/users"),
            "create_user": EndpointConfig(method="POST", path="/v1/users"),
        },
        limits=ApiLimitsConfig(timeout_seconds=10, max_retries=2),
    )


@pytest.fixture
def bearer_api_config() -> ApiConfig:
    return ApiConfig(
        type="rest",
        base_url="https://bearer.example.com",
        auth=ApiAuthConfig(type="bearer", token_env="BEARER_TOKEN_ENV"),
        endpoints={"ping": EndpointConfig(method="GET", path="/ping")},
    )


@pytest.fixture
def graphql_api_config() -> ApiConfig:
    return ApiConfig(
        type="graphql",
        base_url="https://api.example.com/graphql",
        auth=None,
        endpoints={},
    )


@pytest.fixture
async def recorder(tmp_path: Path):
    config = WriterConfig(
        log_dir=tmp_path / "logs",
        retention_days=1,
        enabled_categories=frozenset(Category),
    )
    rec = Recorder(JsonlWriter(config))
    await rec.start()
    try:
        yield rec
    finally:
        await rec.stop()


@pytest.fixture
def fake_token() -> TokenInfo:
    return TokenInfo(
        access_token="fake_xyz",
        refresh_token="rt",
        expires_at=time.time() + 3600,
    )


@pytest.fixture
def stub_credentials(fake_token: TokenInfo) -> MagicMock:
    """Credentials mock — get returns fake_token, peek returns fake_token by default."""
    creds = MagicMock()
    creds.get = AsyncMock(return_value=fake_token)
    creds.peek = AsyncMock(return_value=fake_token)
    return creds


@pytest.fixture
def make_rest_response() -> Callable[..., httpx.Response]:
    def _make(
        status: int = 200,
        json_body: Any = None,
        content_type: str = "application/json",
    ) -> httpx.Response:
        import json as _json

        body = _json.dumps(json_body if json_body is not None else {}).encode()
        return httpx.Response(
            status_code=status,
            headers={"content-type": content_type},
            content=body,
        )

    return _make


@pytest.fixture
def context_factory(recorder: Recorder, stub_credentials: MagicMock) -> Callable[..., ToolContext]:
    """Build a ToolContext with a queue-driven REST/GraphQL client.

    Tests can call ctx = context_factory({api_id: cfg}, rest_responses=[...]) to
    pre-program responses returned by the rest_client_factory's request method.
    """

    def _build(
        configs: dict[str, ApiConfig],
        *,
        rest_responses: list[httpx.Response] | None = None,
        graphql_responses: list[httpx.Response] | None = None,
        rest_request_log: list[dict[str, Any]] | None = None,
        graphql_request_log: list[dict[str, Any]] | None = None,
    ) -> ToolContext:
        rest_q = list(rest_responses or [])
        gql_q = list(graphql_responses or [])

        async def rest_request(
            method: str,
            path: str,
            *,
            params: Any = None,
            json: Any = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            if rest_request_log is not None:
                rest_request_log.append(
                    {
                        "method": method,
                        "path": path,
                        "params": params,
                        "json": json,
                        "headers": dict(headers or {}),
                    }
                )
            if not rest_q:
                return httpx.Response(status_code=200, content=b"{}")
            return rest_q.pop(0)

        async def gql_execute(
            query: str,
            *,
            variables: Any = None,
            operation_name: str | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            if graphql_request_log is not None:
                graphql_request_log.append(
                    {
                        "query": query,
                        "variables": variables,
                        "operation_name": operation_name,
                        "headers": dict(headers or {}),
                    }
                )
            if not gql_q:
                return httpx.Response(status_code=200, content=b'{"data": {}}')
            return gql_q.pop(0)

        rest_client = MagicMock(spec=RestClient)
        rest_client.request = rest_request

        gql_client = MagicMock(spec=GraphQLClient)
        gql_client.execute = gql_execute

        return ToolContext(
            configs=configs,
            credentials=stub_credentials,
            rest_client_factory=lambda _cfg: rest_client,
            graphql_client_factory=lambda _cfg: gql_client,
            recorder=recorder,
        )

    return _build


# ---------------------------------------------------------------------------
# fetch_data
# ---------------------------------------------------------------------------


async def test_fetch_data_happy_path(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
) -> None:
    request_log: list[dict[str, Any]] = []
    ctx = context_factory(
        {"example": rest_api_config},
        rest_responses=[make_rest_response(200, {"users": [{"id": 1}]})],
        rest_request_log=request_log,
    )
    result = await fetch_data(
        FetchDataInput(api_id="example", endpoint="list_users", filters={"limit": 5}),
        context=ctx,
    )
    assert "data" in result
    assert result["data"] == {"users": [{"id": 1}]}
    assert result["metadata"]["source"] == "example"

    assert request_log[0]["method"] == "GET"
    assert request_log[0]["path"] == "/v1/users"
    assert request_log[0]["params"] == {"limit": 5}
    assert request_log[0]["headers"]["Authorization"] == "Bearer fake_xyz"


async def test_fetch_data_unknown_api_returns_api_not_configured(
    context_factory: Callable[..., ToolContext],
) -> None:
    ctx = context_factory({})
    result = await fetch_data(FetchDataInput(api_id="missing", endpoint="x"), context=ctx)
    assert result["error"]["code"] == "API_NOT_CONFIGURED"


async def test_fetch_data_unknown_endpoint_returns_endpoint_not_found(
    rest_api_config: ApiConfig, context_factory: Callable[..., ToolContext]
) -> None:
    ctx = context_factory({"example": rest_api_config})
    result = await fetch_data(FetchDataInput(api_id="example", endpoint="nope"), context=ctx)
    assert result["error"]["code"] == "ENDPOINT_NOT_FOUND"


async def test_fetch_data_auth_required_when_credentials_raise(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    stub_credentials: MagicMock,
) -> None:
    stub_credentials.get = AsyncMock(side_effect=AuthRequiredError("no creds"))
    ctx = context_factory({"example": rest_api_config})
    result = await fetch_data(FetchDataInput(api_id="example", endpoint="list_users"), context=ctx)
    assert result["error"]["code"] == "AUTH_REQUIRED"
    # Reason in details is the exception class name, not the raw message.
    assert result["error"]["details"]["reason"] == "AuthRequiredError"


async def test_fetch_data_rejects_graphql_api(
    graphql_api_config: ApiConfig, context_factory: Callable[..., ToolContext]
) -> None:
    ctx = context_factory({"gql": graphql_api_config})
    result = await fetch_data(FetchDataInput(api_id="gql", endpoint="x"), context=ctx)
    assert result["error"]["code"] == "VALIDATION_ERROR"


async def test_fetch_data_with_bearer_auth_uses_env_token(
    bearer_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEARER_TOKEN_ENV", "env_bearer_value")
    request_log: list[dict[str, Any]] = []
    ctx = context_factory(
        {"bearer": bearer_api_config},
        rest_responses=[make_rest_response(200, {"ok": True})],
        rest_request_log=request_log,
    )
    result = await fetch_data(FetchDataInput(api_id="bearer", endpoint="ping"), context=ctx)
    assert "data" in result
    assert request_log[0]["headers"]["Authorization"] == "Bearer env_bearer_value"


async def test_fetch_data_bearer_missing_env_returns_auth_required(
    bearer_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BEARER_TOKEN_ENV", raising=False)
    ctx = context_factory({"bearer": bearer_api_config})
    result = await fetch_data(FetchDataInput(api_id="bearer", endpoint="ping"), context=ctx)
    assert result["error"]["code"] == "AUTH_REQUIRED"


# ---------------------------------------------------------------------------
# send_data
# ---------------------------------------------------------------------------


async def test_send_data_posts_payload_with_method_from_endpoint(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
) -> None:
    request_log: list[dict[str, Any]] = []
    ctx = context_factory(
        {"example": rest_api_config},
        rest_responses=[make_rest_response(201, {"id": 9})],
        rest_request_log=request_log,
    )
    result = await send_data(
        SendDataInput(
            api_id="example",
            endpoint="create_user",
            payload={"name": "Ada", "email": "ada@example.com"},
        ),
        context=ctx,
    )
    assert "error" not in result
    assert request_log[0]["method"] == "POST"
    assert request_log[0]["json"] == {"name": "Ada", "email": "ada@example.com"}


# ---------------------------------------------------------------------------
# execute_graphql
# ---------------------------------------------------------------------------


async def test_execute_graphql_happy_path(
    graphql_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
) -> None:
    request_log: list[dict[str, Any]] = []
    ctx = context_factory(
        {"gql": graphql_api_config},
        graphql_responses=[make_rest_response(200, {"data": {"viewer": {"id": "u1"}}})],
        graphql_request_log=request_log,
    )
    result = await execute_graphql(
        GraphQLInput(
            api_id="gql",
            query="query Q { viewer { id } }",
            operation_name="Q",
        ),
        context=ctx,
    )
    assert result["data"] == {"viewer": {"id": "u1"}}
    assert request_log[0]["operation_name"] == "Q"


async def test_execute_graphql_partial_success_preserved(
    graphql_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
) -> None:
    ctx = context_factory(
        {"gql": graphql_api_config},
        graphql_responses=[
            make_rest_response(
                200,
                {
                    "data": {"viewer": {"id": "u1"}},
                    "errors": [{"message": "field unauthorized"}],
                },
            )
        ],
    )
    result = await execute_graphql(
        GraphQLInput(api_id="gql", query="query { viewer { id } }"), context=ctx
    )
    assert result["data"] == {"viewer": {"id": "u1"}}
    assert result["errors"][0]["message"] == "field unauthorized"
    assert "error" not in result  # NOT collapsed


async def test_execute_graphql_rejects_rest_api(
    rest_api_config: ApiConfig, context_factory: Callable[..., ToolContext]
) -> None:
    ctx = context_factory({"example": rest_api_config})
    result = await execute_graphql(GraphQLInput(api_id="example", query="query {}"), context=ctx)
    assert result["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# get_status — must NOT trigger OAuth (peek only)
# ---------------------------------------------------------------------------


async def test_get_status_lists_all_apis(
    rest_api_config: ApiConfig,
    bearer_api_config: ApiConfig,
    graphql_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
) -> None:
    ctx = context_factory(
        {
            "oauth_api": rest_api_config,
            "bearer_api": bearer_api_config,
            "noauth": graphql_api_config,
        }
    )
    result = await get_status(GetStatusInput(), context=ctx)
    states = {item["api_id"]: item["auth_state"] for item in result["data"]}
    assert states["oauth_api"] == "authenticated"  # peek returns fake_token
    assert states["noauth"] == "not_required"


async def test_get_status_uses_peek_not_get(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    stub_credentials: MagicMock,
) -> None:
    """get_status must NEVER call credentials.get (which can refresh / open browser)."""
    ctx = context_factory({"example": rest_api_config})
    await get_status(GetStatusInput(api_id="example"), context=ctx)

    stub_credentials.peek.assert_called()
    stub_credentials.get.assert_not_called()


async def test_get_status_reflects_expired_oauth_without_refresh(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    stub_credentials: MagicMock,
) -> None:
    expired = TokenInfo(
        access_token="dead",
        refresh_token="rt",
        expires_at=time.time() - 60,  # already expired
    )
    stub_credentials.peek = AsyncMock(return_value=expired)

    ctx = context_factory({"example": rest_api_config})
    result = await get_status(GetStatusInput(api_id="example"), context=ctx)

    assert result["data"][0]["auth_state"] == "expired"
    stub_credentials.get.assert_not_called()


async def test_get_status_does_not_invoke_oauth_start_flow(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    stub_credentials: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stronger assertion: even with an OAuth instance reachable, start_flow is never called."""
    spy = AsyncMock(side_effect=AssertionError("OAuth flow must not be triggered"))
    monkeypatch.setattr(OAuth, "start_flow", spy)
    stub_credentials.peek = AsyncMock(return_value=None)  # no token stored

    ctx = context_factory({"example": rest_api_config})
    result = await get_status(GetStatusInput(api_id="example"), context=ctx)

    assert result["data"][0]["auth_state"] == "unauthenticated"
    spy.assert_not_called()


@pytest.mark.parametrize(
    "env_set, expected_state", [(True, "authenticated"), (False, "unauthenticated")]
)
async def test_get_status_bearer_branch(
    bearer_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    monkeypatch: pytest.MonkeyPatch,
    env_set: bool,
    expected_state: str,
) -> None:
    """Bearer auth: state mirrors token_env presence; never touches Credentials."""
    if env_set:
        monkeypatch.setenv("BEARER_TOKEN_ENV", "tok_value")
    else:
        monkeypatch.delenv("BEARER_TOKEN_ENV", raising=False)

    ctx = context_factory({"bearer": bearer_api_config})
    result = await get_status(GetStatusInput(api_id="bearer"), context=ctx)

    assert result["data"][0]["auth_state"] == expected_state
    # Bearer path must NOT consult Credentials at all.
    ctx.credentials.peek.assert_not_called()  # type: ignore[attr-defined]
    ctx.credentials.get.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "env_set, expected_state", [(True, "authenticated"), (False, "unauthenticated")]
)
async def test_get_status_api_key_branch(
    context_factory: Callable[..., ToolContext],
    monkeypatch: pytest.MonkeyPatch,
    env_set: bool,
    expected_state: str,
) -> None:
    """api_key auth: state mirrors key_env presence."""
    api_key_config = ApiConfig(
        type="rest",
        base_url="https://k.example.com",
        auth=ApiAuthConfig(type="api_key", header_name="X-API-Key", key_env="DEMO_API_KEY"),
        endpoints={},
    )
    if env_set:
        monkeypatch.setenv("DEMO_API_KEY", "the_key")
    else:
        monkeypatch.delenv("DEMO_API_KEY", raising=False)

    ctx = context_factory({"k": api_key_config})
    result = await get_status(GetStatusInput(api_id="k"), context=ctx)

    assert result["data"][0]["auth_state"] == expected_state


async def test_get_status_unknown_auth_type_returns_unknown(
    context_factory: Callable[..., ToolContext],
) -> None:
    weird_config = ApiConfig(
        type="rest",
        base_url="https://w.example.com",
        auth=ApiAuthConfig(type="totally_made_up"),
        endpoints={},
    )
    ctx = context_factory({"w": weird_config})
    result = await get_status(GetStatusInput(api_id="w"), context=ctx)
    assert result["data"][0]["auth_state"] == "unknown"


# ---------------------------------------------------------------------------
# Pydantic input validation via *_handler wrappers
# ---------------------------------------------------------------------------


async def test_handler_invalid_arguments_returns_validation_error(
    rest_api_config: ApiConfig, context_factory: Callable[..., ToolContext]
) -> None:
    ctx = context_factory({"example": rest_api_config})
    # Missing required `endpoint`.
    result = await fetch_data_handler({"api_id": "example"}, context=ctx)
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert "field_errors" in result["error"]["details"]


async def test_handler_extra_fields_rejected(
    rest_api_config: ApiConfig, context_factory: Callable[..., ToolContext]
) -> None:
    ctx = context_factory({"example": rest_api_config})
    result = await fetch_data_handler(
        {"api_id": "example", "endpoint": "list_users", "extra": "boom"},
        context=ctx,
    )
    assert result["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Recorder triple is written for every code path (success + error)
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    import json as _json

    if not path.exists():
        return []
    return [_json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _month_files(log_root: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for category in ("audit", "usage", "insight"):
        cat_dir = log_root / category
        if not cat_dir.exists():
            result[category] = []
            continue
        events: list[dict[str, Any]] = []
        for f in sorted(cat_dir.iterdir()):
            events.extend(_read_jsonl(f))
        result[category] = events
    return result


async def test_successful_call_writes_audit_usage_insight(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
    tmp_path: Path,
    recorder: Recorder,
) -> None:
    ctx = context_factory(
        {"example": rest_api_config},
        rest_responses=[make_rest_response(200, {"x": 1})],
    )
    await fetch_data(FetchDataInput(api_id="example", endpoint="list_users"), context=ctx)
    # Recorder writer flushes on stop; trigger via fixture teardown later, but we
    # can assert by stopping the recorder here is overkill — instead, give the
    # writer a tick to flush the buffer (buffer_size default = 100, flush_interval=5s).
    # Public Recorder.stop() drains the writer queue and is idempotent — the
    # fixture's teardown will call it again as a no-op.
    await recorder.stop()

    events = _month_files(tmp_path / "logs")
    audit = [e for e in events["audit"] if e.get("tool") == "fetch_data"]
    usage = [e for e in events["usage"] if e.get("tool") == "fetch_data"]
    insight = [e for e in events["insight"] if e.get("tool") == "fetch_data"]
    assert len(audit) == 1
    assert audit[0]["result"] == "success"
    assert len(usage) == 1
    assert len(insight) == 1


async def test_failure_path_still_writes_audit_usage_insight(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    stub_credentials: MagicMock,
    tmp_path: Path,
    recorder: Recorder,
) -> None:
    stub_credentials.get = AsyncMock(side_effect=AuthRequiredError("no token"))
    ctx = context_factory({"example": rest_api_config})

    result = await fetch_data(FetchDataInput(api_id="example", endpoint="list_users"), context=ctx)
    assert result["error"]["code"] == "AUTH_REQUIRED"

    await recorder.stop()
    events = _month_files(tmp_path / "logs")
    audit = [e for e in events["audit"] if e.get("tool") == "fetch_data"]
    assert len(audit) == 1
    assert audit[0]["result"] == "error"


# ---------------------------------------------------------------------------
# tool_args redaction in record_insight (no secrets in JSONL)
# ---------------------------------------------------------------------------


async def test_send_data_payload_secrets_redacted_in_insight(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
    tmp_path: Path,
    recorder: Recorder,
) -> None:
    ctx = context_factory(
        {"example": rest_api_config},
        rest_responses=[make_rest_response(201, {"id": 1})],
    )
    await send_data(
        SendDataInput(
            api_id="example",
            endpoint="create_user",
            payload={"name": "Ada", "api_key": "SECRET_API_KEY_42", "password": "hunter2"},
        ),
        context=ctx,
    )

    await recorder.stop()
    events = _month_files(tmp_path / "logs")
    insight = [e for e in events["insight"] if e.get("tool") == "send_data"]
    assert len(insight) == 1
    raw = str(insight[0])
    assert "SECRET_API_KEY_42" not in raw
    assert "hunter2" not in raw
    assert "<redacted>" in raw


# ---------------------------------------------------------------------------
# Server-facing handler wrappers actually delegate
# ---------------------------------------------------------------------------


async def test_send_data_handler_routes_to_send_data(
    rest_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
) -> None:
    ctx = context_factory(
        {"example": rest_api_config},
        rest_responses=[make_rest_response(201, {"id": 1})],
    )
    result = await send_data_handler(
        {"api_id": "example", "endpoint": "create_user", "payload": {"x": 1}},
        context=ctx,
    )
    assert "error" not in result


async def test_execute_graphql_handler_routes_to_execute_graphql(
    graphql_api_config: ApiConfig,
    context_factory: Callable[..., ToolContext],
    make_rest_response: Callable[..., httpx.Response],
) -> None:
    ctx = context_factory(
        {"gql": graphql_api_config},
        graphql_responses=[make_rest_response(200, {"data": {"v": 1}})],
    )
    result = await execute_graphql_handler({"api_id": "gql", "query": "query { v }"}, context=ctx)
    assert result["data"] == {"v": 1}


# Silence ruff "unused import" — these are used by fixture-typed parameters.
_ = OAuthConfig
