from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from src.events.redaction import redact_body, redact_headers

DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB

_BINARY_CONTENT_TYPE_PREFIXES: tuple[str, ...] = (
    "image/",
    "video/",
    "audio/",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/x-",
)

_BODY_EXCERPT_BYTES = 512  # length of error.details.body_excerpt


class GatewayError(RuntimeError):
    """Base for gateway-internal errors before they become {error: ...} dicts."""


class ResponseTooLargeError(GatewayError):
    """Binary or streaming response exceeded MCP_MAX_RESPONSE_BYTES."""


# ---------------------------------------------------------------------------
# Public API — REST normalizer
# ---------------------------------------------------------------------------


def normalize_rest_response(
    *,
    api_id: str,
    endpoint: str,
    response: httpx.Response,
    started_at: float,
) -> dict[str, Any]:
    """Convert an httpx.Response into the project's standard dict shape.

    Success (2xx)  → {"data": ..., "metadata": {...}}
    Error (4xx/5xx) → {"error": {"code": ..., "message": ..., "details": {...}}}

    See CLAUDE.md § Response Format Conventions for the contract.
    """
    duration_ms = int((time.time() - started_at) * 1000)
    metadata: dict[str, Any] = {
        "source": api_id,
        "endpoint": endpoint,
        "timestamp": _iso_utc_now(),
        "duration_ms": duration_ms,
        "status_code": response.status_code,
    }
    _add_rate_limit_metadata(metadata, response.headers)

    if 200 <= response.status_code < 300:
        return _success_payload(response, metadata)
    return _error_payload(response, api_id, endpoint)


# ---------------------------------------------------------------------------
# Public API — GraphQL normalizer
# ---------------------------------------------------------------------------


def normalize_graphql_response(
    *,
    api_id: str,
    response: httpx.Response,
    started_at: float,
) -> dict[str, Any]:
    """Parse a GraphQL response body, surfacing partial-success when present.

    GraphQL endpoints return HTTP 200 even when there are errors. We MUST inspect
    the body and surface both `data` and `errors` when both exist — collapsing
    partial success into a flat error loses information Claude can still use.
    """
    raw_bytes = response.content
    total_bytes = len(raw_bytes)
    max_bytes = _max_response_bytes()

    if total_bytes > max_bytes:
        # GraphQL bodies are JSON; we could truncate, but the data field would be
        # half-parsed. Return RESPONSE_TOO_LARGE with hint to use pagination/filters.
        return _too_large_error(api_id, "graphql", total_bytes, max_bytes)

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _build_error(
            code="UPSTREAM_ERROR",
            message=f"GraphQL response was not valid JSON: {type(exc).__name__}",
            details={"status_code": response.status_code},
        )

    if not isinstance(body, dict):
        return _build_error(
            code="UPSTREAM_ERROR",
            message="GraphQL response body is not a JSON object",
            details={"status_code": response.status_code},
        )

    data = body.get("data")
    errors = body.get("errors")

    if errors and data is None:
        return _build_error(
            code="UPSTREAM_ERROR",
            message="GraphQL request failed",
            details={"status_code": response.status_code, "graphql_errors": errors},
        )

    # Build metadata only on the success / partial-success paths — error paths above
    # use _build_error / _too_large_error which return {"error": {...}} only.
    duration_ms = int((time.time() - started_at) * 1000)
    metadata: dict[str, Any] = {
        "source": api_id,
        "endpoint": "graphql",
        "timestamp": _iso_utc_now(),
        "duration_ms": duration_ms,
        "status_code": response.status_code,
        "returned_bytes": total_bytes,
        "total_bytes": total_bytes,
    }
    _add_rate_limit_metadata(metadata, response.headers)

    if errors and data is not None:
        # Partial success — surface BOTH. Do not collapse into an error.
        return {"data": data, "errors": errors, "metadata": metadata}
    return {"data": data, "metadata": metadata}


# ---------------------------------------------------------------------------
# Internal helpers — success payloads
# ---------------------------------------------------------------------------


def _success_payload(response: httpx.Response, metadata: dict[str, Any]) -> dict[str, Any]:
    raw_bytes = response.content
    total_bytes = len(raw_bytes)
    max_bytes = _max_response_bytes()
    content_type = (response.headers.get("content-type") or "").lower()

    metadata["total_bytes"] = total_bytes

    if _is_binary_content_type(content_type):
        if total_bytes > max_bytes:
            return _too_large_error(
                metadata["source"], metadata["endpoint"], total_bytes, max_bytes
            )
        import base64

        metadata["returned_bytes"] = total_bytes
        metadata["content_type"] = content_type or "application/octet-stream"
        metadata["encoding"] = "base64"
        return {"data": base64.b64encode(raw_bytes).decode("ascii"), "metadata": metadata}

    # Text / JSON path.
    if total_bytes > max_bytes:
        truncated = raw_bytes[:max_bytes].decode("utf-8", errors="replace")
        metadata["returned_bytes"] = max_bytes
        metadata["truncated"] = True
        metadata["hint"] = (
            "Response truncated at MCP_MAX_RESPONSE_BYTES. "
            "Use pagination/filters or raise the limit."
        )
        return {"data": truncated, "metadata": metadata}

    metadata["returned_bytes"] = total_bytes

    if "json" in content_type:
        try:
            data: Any = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = raw_bytes.decode("utf-8", errors="replace")
    else:
        data = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""

    return {"data": data, "metadata": metadata}


# ---------------------------------------------------------------------------
# Internal helpers — error payloads
# ---------------------------------------------------------------------------


def _error_payload(
    response: httpx.Response,
    api_id: str,
    endpoint: str,
) -> dict[str, Any]:
    code = _status_to_error_code(response.status_code)
    body_excerpt = _safe_body_excerpt(response)
    details: dict[str, Any] = {
        "status_code": response.status_code,
        "body_excerpt": body_excerpt,
    }
    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            details["retry_after"] = retry_after
    # Per CLAUDE.md § Response Format Conventions, error responses are {error} only —
    # no top-level metadata sibling. Useful debugging context lives in error.details
    # and (for operators) in the Recorder audit/usage events Phase 5 will write.
    details["source"] = api_id
    details["endpoint"] = endpoint
    message = f"Upstream API returned HTTP {response.status_code}"
    return {"error": {"code": code, "message": message, "details": details}}


def _too_large_error(
    api_id: str,
    endpoint: str,
    total_bytes: int,
    max_bytes: int,
) -> dict[str, Any]:
    return {
        "error": {
            "code": "RESPONSE_TOO_LARGE",
            "message": (
                f"Response size {total_bytes} bytes exceeds limit {max_bytes} bytes "
                f"and cannot be safely truncated."
            ),
            "details": {
                "total_bytes": total_bytes,
                "max_bytes": max_bytes,
                "source": api_id,
                "endpoint": endpoint,
                "hint": "Use pagination/filters or raise MCP_MAX_RESPONSE_BYTES.",
            },
        },
    }


def _build_error(*, code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


# ---------------------------------------------------------------------------
# Status-code mapping
# ---------------------------------------------------------------------------


def _status_to_error_code(status: int) -> str:
    if status in (401, 403):
        return "AUTH_REQUIRED"
    if status == 404:
        return "ENDPOINT_NOT_FOUND"
    if status == 422:
        return "VALIDATION_ERROR"
    if status == 429:
        return "RATE_LIMITED"
    return "UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# Header / metadata helpers
# ---------------------------------------------------------------------------


def _add_rate_limit_metadata(
    metadata: dict[str, Any], headers: httpx.Headers | dict[str, str]
) -> None:
    """Surface common rate-limit headers (when present) into metadata."""
    # httpx.Headers is case-insensitive; plain dict isn't — normalize via .get.
    remaining = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
    reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if remaining is not None:
        metadata["rate_limit_remaining"] = remaining
    if reset is not None:
        metadata["rate_limit_reset"] = reset


def _is_binary_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    head = content_type.split(";", 1)[0].strip().lower()
    return any(head.startswith(prefix) for prefix in _BINARY_CONTENT_TYPE_PREFIXES)


def _safe_body_excerpt(response: httpx.Response) -> str:
    """Return a small redacted excerpt of the response body for error.details.

    Always passes through `redact_body` if the body parses as JSON, so secret keys
    in error responses are masked. Plain text is truncated only.
    """
    raw = response.content[: _BODY_EXCERPT_BYTES * 4]  # decode some slack for multi-byte
    text = raw.decode("utf-8", errors="replace")
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text[:_BODY_EXCERPT_BYTES]
        redacted = redact_body(parsed)
        return json.dumps(redacted)[:_BODY_EXCERPT_BYTES]
    return text[:_BODY_EXCERPT_BYTES]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_response_bytes() -> int:
    raw = os.getenv("MCP_MAX_RESPONSE_BYTES")
    if raw is None:
        return DEFAULT_MAX_RESPONSE_BYTES
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_RESPONSE_BYTES


# Keep redact_headers reachable from this module for tests / future hooks; it
# is intentionally re-exported so consumers don't have to learn two paths.
__all__ = [
    "GatewayError",
    "ResponseTooLargeError",
    "normalize_graphql_response",
    "normalize_rest_response",
    "redact_body",
    "redact_headers",
]
