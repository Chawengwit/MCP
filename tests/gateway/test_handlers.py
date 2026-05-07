from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from src.gateway.handlers import (
    normalize_graphql_response,
    normalize_rest_response,
)

from tests.gateway.conftest import binary_response, json_response, text_response

# ---------------------------------------------------------------------------
# REST normalizer — happy path
# ---------------------------------------------------------------------------


def test_rest_2xx_json_returns_data_and_metadata() -> None:
    response = json_response(200, {"users": [{"id": 1}]})
    result = normalize_rest_response(
        api_id="example",
        endpoint="get_users",
        response=response,
        started_at=time.time(),
    )
    assert result["data"] == {"users": [{"id": 1}]}
    md = result["metadata"]
    assert md["source"] == "example"
    assert md["endpoint"] == "get_users"
    assert md["status_code"] == 200
    assert "duration_ms" in md
    assert "timestamp" in md
    assert md["returned_bytes"] == md["total_bytes"]


def test_rest_2xx_text_returns_string_data() -> None:
    response = text_response(200, "hello world")
    result = normalize_rest_response(
        api_id="example",
        endpoint="ping",
        response=response,
        started_at=time.time(),
    )
    assert result["data"] == "hello world"
    assert "error" not in result


def test_rest_2xx_binary_returns_base64_with_metadata() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    response = binary_response(200, content=payload, content_type="image/png")
    result = normalize_rest_response(
        api_id="example",
        endpoint="get_image",
        response=response,
        started_at=time.time(),
    )
    assert "data" in result
    assert base64.b64decode(result["data"]) == payload
    md = result["metadata"]
    assert md["content_type"].startswith("image/png")
    assert md["encoding"] == "base64"


# ---------------------------------------------------------------------------
# Status-code mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected_code",
    [
        (401, "AUTH_REQUIRED"),
        (403, "AUTH_REQUIRED"),
        (404, "ENDPOINT_NOT_FOUND"),
        (422, "VALIDATION_ERROR"),
        (429, "RATE_LIMITED"),
        (500, "UPSTREAM_ERROR"),
        (502, "UPSTREAM_ERROR"),
        (503, "UPSTREAM_ERROR"),
        (504, "UPSTREAM_ERROR"),
    ],
)
def test_status_code_to_error_code_mapping(status: int, expected_code: str) -> None:
    response = httpx.Response(
        status_code=status,
        headers={"content-type": "application/json"},
        content=b'{"error": "boom"}',
    )
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["code"] == expected_code
    assert result["error"]["details"]["status_code"] == status


def test_429_includes_retry_after_in_details() -> None:
    response = httpx.Response(
        status_code=429,
        headers={"content-type": "application/json", "retry-after": "60"},
        content=b'{"error":"slow down"}',
    )
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["details"]["retry_after"] == "60"


# ---------------------------------------------------------------------------
# Error response shape conforms to CLAUDE.md (no top-level metadata sibling)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 404, 422, 429, 500, 503])
def test_error_response_has_no_top_level_metadata(status: int) -> None:
    """Per CLAUDE.md § Response Format Conventions, error responses are {error} only."""
    response = httpx.Response(
        status_code=status,
        headers={"content-type": "application/json"},
        content=b'{"err":"x"}',
    )
    result = normalize_rest_response(
        api_id="example",
        endpoint="get_users",
        response=response,
        started_at=time.time(),
    )
    assert "metadata" not in result
    assert set(result.keys()) == {"error"}


def test_error_details_include_source_and_endpoint() -> None:
    response = httpx.Response(
        status_code=500,
        headers={"content-type": "application/json"},
        content=b'{"err":"x"}',
    )
    result = normalize_rest_response(
        api_id="example_api",
        endpoint="get_things",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["details"]["source"] == "example_api"
    assert result["error"]["details"]["endpoint"] == "get_things"


def test_response_too_large_error_has_no_top_level_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_MAX_RESPONSE_BYTES", "10")
    response = binary_response(200, content=b"\x00" * 100, content_type="image/png")
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    assert "metadata" not in result
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert result["error"]["details"]["source"] == "example"
    assert result["error"]["details"]["endpoint"] == "x"


# ---------------------------------------------------------------------------
# Body excerpt redaction in error responses
# ---------------------------------------------------------------------------


def test_error_body_excerpt_redacts_secret_keys() -> None:
    leaky = json.dumps({"access_token": "EXPOSED_TOKEN", "msg": "expired"}).encode()
    response = httpx.Response(
        status_code=401,
        headers={"content-type": "application/json"},
        content=leaky,
    )
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    excerpt = result["error"]["details"]["body_excerpt"]
    assert "EXPOSED_TOKEN" not in excerpt
    assert "<redacted>" in excerpt


# ---------------------------------------------------------------------------
# Rate-limit metadata extraction
# ---------------------------------------------------------------------------


def test_rate_limit_headers_surfaced_in_metadata() -> None:
    response = httpx.Response(
        status_code=200,
        headers={
            "content-type": "application/json",
            "x-ratelimit-remaining": "42",
            "x-ratelimit-reset": "1700000000",
        },
        content=b"{}",
    )
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    assert result["metadata"]["rate_limit_remaining"] == "42"
    assert result["metadata"]["rate_limit_reset"] == "1700000000"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_oversized_text_response_truncates_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_MAX_RESPONSE_BYTES", "100")
    big = "x" * 500
    response = text_response(200, big)
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    assert "data" in result
    assert len(result["data"]) == 100
    md = result["metadata"]
    assert md["truncated"] is True
    assert md["total_bytes"] == 500
    assert md["returned_bytes"] == 100
    assert "hint" in md


def test_oversized_binary_response_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_MAX_RESPONSE_BYTES", "100")
    response = binary_response(200, content=b"\x00" * 500, content_type="image/png")
    result = normalize_rest_response(
        api_id="example",
        endpoint="x",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert result["error"]["details"]["total_bytes"] == 500
    assert result["error"]["details"]["max_bytes"] == 100


# ---------------------------------------------------------------------------
# GraphQL normalizer
# ---------------------------------------------------------------------------


def test_graphql_data_only_returns_data_and_metadata() -> None:
    response = json_response(200, {"data": {"viewer": {"id": "u1"}}})
    result = normalize_graphql_response(
        api_id="example",
        response=response,
        started_at=time.time(),
    )
    assert result["data"] == {"viewer": {"id": "u1"}}
    assert "errors" not in result
    assert result["metadata"]["source"] == "example"


def test_graphql_partial_success_surfaces_data_and_errors() -> None:
    body = {
        "data": {"viewer": {"id": "u1"}},
        "errors": [{"message": "field unauthorized", "path": ["viewer", "secret"]}],
    }
    response = json_response(200, body)
    result = normalize_graphql_response(
        api_id="example",
        response=response,
        started_at=time.time(),
    )
    assert result["data"] == {"viewer": {"id": "u1"}}
    assert result["errors"][0]["message"] == "field unauthorized"
    assert "error" not in result  # NOT collapsed into a flat error


def test_graphql_errors_only_returns_error_shape() -> None:
    body = {"errors": [{"message": "syntax error"}]}
    response = json_response(200, body)
    result = normalize_graphql_response(
        api_id="example",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["code"] == "UPSTREAM_ERROR"
    assert result["error"]["details"]["graphql_errors"][0]["message"] == "syntax error"


def test_graphql_invalid_json_returns_upstream_error() -> None:
    response = httpx.Response(
        status_code=200,
        headers={"content-type": "application/json"},
        content=b"not json {",
    )
    result = normalize_graphql_response(
        api_id="example",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["code"] == "UPSTREAM_ERROR"


def test_graphql_oversized_response_returns_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_MAX_RESPONSE_BYTES", "50")
    body = {"data": {"big": "x" * 500}}
    response = json_response(200, body)
    result = normalize_graphql_response(
        api_id="example",
        response=response,
        started_at=time.time(),
    )
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
