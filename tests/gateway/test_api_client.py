from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from src.gateway.api_client import RestClient, _compute_backoff

from tests.gateway.conftest import MockTransport, json_response

# ---------------------------------------------------------------------------
# Method matrix — each verb sends the expected request line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
async def test_each_http_method_succeeds(method: str, mock_transport: MockTransport) -> None:
    mock_transport.queue(json_response(200, {"ok": True}))
    client = RestClient(base_url="https://api.example.com")
    response = await client.request(method, "/v1/things")
    assert response.status_code == 200
    assert mock_transport.requests[0].method == method
    assert str(mock_transport.requests[0].url) == "https://api.example.com/v1/things"


# ---------------------------------------------------------------------------
# URL joining
# ---------------------------------------------------------------------------


async def test_relative_path_joins_with_base_url(mock_transport: MockTransport) -> None:
    mock_transport.queue(json_response(200, {}))
    client = RestClient(base_url="https://api.example.com/")
    await client.request("GET", "users/42")
    assert str(mock_transport.requests[0].url) == "https://api.example.com/users/42"


async def test_absolute_url_path_overrides_base(mock_transport: MockTransport) -> None:
    mock_transport.queue(json_response(200, {}))
    client = RestClient(base_url="https://api.example.com")
    await client.request("GET", "https://other.example.com/x")
    assert str(mock_transport.requests[0].url) == "https://other.example.com/x"


# ---------------------------------------------------------------------------
# Header precedence — defaults < auth_provider < per-request
# ---------------------------------------------------------------------------


async def test_per_request_headers_appear_in_outbound_request(
    mock_transport: MockTransport,
) -> None:
    mock_transport.queue(json_response(200, {}))
    client = RestClient(base_url="https://api.example.com")
    await client.request("GET", "/x", headers={"X-Custom": "val"})
    assert mock_transport.requests[0].headers["X-Custom"] == "val"


async def test_auth_provider_headers_merge_when_no_override(
    mock_transport: MockTransport,
    mock_auth_provider: Callable[[], Awaitable[dict[str, str]]],
) -> None:
    mock_transport.queue(json_response(200, {}))
    client = RestClient(base_url="https://api.example.com", auth_provider=mock_auth_provider)
    await client.request("GET", "/x")
    assert mock_transport.requests[0].headers["Authorization"] == "Bearer auth_provider_token"


async def test_per_request_header_overrides_auth_provider(
    mock_transport: MockTransport,
    mock_auth_provider: Callable[[], Awaitable[dict[str, str]]],
) -> None:
    mock_transport.queue(json_response(200, {}))
    client = RestClient(base_url="https://api.example.com", auth_provider=mock_auth_provider)
    await client.request("GET", "/x", headers={"Authorization": "Bearer override"})
    assert mock_transport.requests[0].headers["Authorization"] == "Bearer override"


async def test_auth_provider_failure_does_not_crash_request(
    mock_transport: MockTransport,
) -> None:
    mock_transport.queue(json_response(200, {}))

    async def bad_provider() -> dict[str, str]:
        raise RuntimeError("auth backend down")

    client = RestClient(base_url="https://api.example.com", auth_provider=bad_provider)
    response = await client.request("GET", "/x")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


async def test_429_is_retried_and_eventually_succeeds(mock_transport: MockTransport) -> None:
    mock_transport.queue(
        httpx.Response(status_code=429, headers={"retry-after": "0"}, content=b""),
        json_response(200, {"ok": True}),
    )
    client = RestClient(base_url="https://api.example.com", max_retries=3)
    response = await client.request("GET", "/x")
    assert response.status_code == 200
    assert len(mock_transport.requests) == 2  # 1 retry


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_retryable_5xx_statuses_are_retried(
    status: int, mock_transport: MockTransport
) -> None:
    mock_transport.queue(
        httpx.Response(status_code=status, content=b""),
        json_response(200, {"ok": True}),
    )
    client = RestClient(base_url="https://api.example.com", max_retries=3)
    response = await client.request("GET", "/x")
    assert response.status_code == 200
    assert len(mock_transport.requests) == 2


async def test_500_is_NOT_retried(mock_transport: MockTransport) -> None:
    mock_transport.queue(httpx.Response(status_code=500, content=b""))
    client = RestClient(base_url="https://api.example.com", max_retries=3)
    response = await client.request("GET", "/x")
    assert response.status_code == 500
    assert len(mock_transport.requests) == 1  # no retry


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_4xx_is_not_retried(status: int, mock_transport: MockTransport) -> None:
    mock_transport.queue(httpx.Response(status_code=status, content=b""))
    client = RestClient(base_url="https://api.example.com", max_retries=3)
    response = await client.request("GET", "/x")
    assert response.status_code == status
    assert len(mock_transport.requests) == 1


async def test_max_retries_bounded(mock_transport: MockTransport) -> None:
    """If every attempt 5xxs, we stop after max_retries+1 total attempts."""
    mock_transport.queue(*[httpx.Response(status_code=503, content=b"") for _ in range(10)])
    client = RestClient(base_url="https://api.example.com", max_retries=2)
    response = await client.request("GET", "/x")
    assert response.status_code == 503
    # 1 initial + 2 retries = 3 total
    assert len(mock_transport.requests) == 3


# ---------------------------------------------------------------------------
# Transport-error retries
# ---------------------------------------------------------------------------


async def test_transport_error_is_retried_then_succeeds(
    patch_async_client_handler: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    """ConnectError on first attempt, 200 on second → request succeeds after retry."""
    call_count = {"n": 0}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient network blip", request=request)
        return httpx.Response(status_code=200, content=b'{"ok": true}', request=request)

    patch_async_client_handler(flaky_handler)

    client = RestClient(base_url="https://api.example.com", max_retries=3)
    response = await client.request("GET", "/x")
    assert response.status_code == 200
    assert call_count["n"] == 2  # one failure + one success


async def test_transport_error_exceeds_retries_raises(
    patch_async_client_handler: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    """When every attempt raises a transport error, the final exception bubbles up."""

    def always_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    patch_async_client_handler(always_fail)

    client = RestClient(base_url="https://api.example.com", max_retries=2)
    with pytest.raises(httpx.ConnectError):
        await client.request("GET", "/x")


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


def test_compute_backoff_honors_retry_after_for_429() -> None:
    response = httpx.Response(status_code=429, headers={"retry-after": "2"})
    delay = _compute_backoff(attempt=0, response=response)
    assert delay == 2.0


def test_compute_backoff_falls_back_when_retry_after_unparseable() -> None:
    response = httpx.Response(status_code=429, headers={"retry-after": "tomorrow"})
    delay = _compute_backoff(attempt=0, response=response)
    assert delay >= 0.5  # base backoff at attempt=0


def test_compute_backoff_capped() -> None:
    response = httpx.Response(status_code=503)
    delay = _compute_backoff(attempt=10, response=response)
    assert delay <= 8.0


def test_compute_backoff_no_response_uses_attempt_only() -> None:
    """Transport-error retry: response=None still computes a sane delay."""
    delay = _compute_backoff(attempt=2)
    assert 2.0 <= delay <= 8.0


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------


async def test_graphql_post_body_shape(mock_transport: MockTransport) -> None:
    from src.gateway.api_client import GraphQLClient

    mock_transport.queue(json_response(200, {"data": {"viewer": {"id": "u1"}}}))
    gql = GraphQLClient(url="https://api.example.com/graphql")
    await gql.execute("query { viewer { id } }", variables={"x": 1}, operation_name="Q")

    sent = mock_transport.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.example.com/graphql"
    body = json.loads(sent.content.decode("utf-8"))
    assert body["query"] == "query { viewer { id } }"
    assert body["variables"] == {"x": 1}
    assert body["operationName"] == "Q"


async def test_graphql_omits_optional_fields_when_none(mock_transport: MockTransport) -> None:
    from src.gateway.api_client import GraphQLClient

    mock_transport.queue(json_response(200, {"data": {}}))
    gql = GraphQLClient(url="https://api.example.com/graphql")
    await gql.execute("query Q { x }")
    body = json.loads(mock_transport.requests[0].content.decode("utf-8"))
    assert body == {"query": "query Q { x }"}


# ---------------------------------------------------------------------------
# Redaction in debug logs (no Bearer token leaks)
# ---------------------------------------------------------------------------


async def test_debug_logs_redact_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
    mock_transport: MockTransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MCP_LOG_DEBUG_ENABLED", "true")
    caplog.set_level("DEBUG", logger="mcp.gateway")
    mock_transport.queue(json_response(200, {}))

    client = RestClient(base_url="https://api.example.com")
    await client.request("GET", "/x", headers={"Authorization": "Bearer SECRET_BEARER_VALUE"})

    assert "SECRET_BEARER_VALUE" not in caplog.text
    # Redacted marker should be present (proves the redaction path ran).
    assert "<redacted>" in caplog.text
