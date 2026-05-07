from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.events.redaction import redact_headers, redact_url

logger = logging.getLogger("mcp.gateway")

DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_MAX_RETRIES = 3
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
# 500 is intentionally NOT retried — it usually indicates a real server bug;
# retrying masks the signal.
RETRYABLE_TRANSPORT_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)
BACKOFF_BASE_SEC = 0.5
BACKOFF_CAP_SEC = 8.0

AuthProvider = Callable[[], Awaitable[dict[str, str]]]


class RestClient:
    """Generic async REST client with retry, optional auth_provider, and redacted logging.

    Header precedence (lowest → highest):
        defaults  →  auth_provider() result (if set)  →  per-request `headers` kwarg

    Phase 5 tools compute headers via `resolve_auth_headers()` and pass them per
    request, so `auth_provider` is OPTIONAL. It's a hook for tests and advanced
    use where constructor-time injection is preferred.

    Note on POST retry: we still retry POST on 429/5xx — the alternative (no
    retry on POST) breaks legitimate transient-failure cases. Callers that need
    strict idempotency should send an `Idempotency-Key` header.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        auth_provider: AuthProvider | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        # base_url defaults to "" so callers (e.g. GraphQLClient) that always pass
        # absolute URLs as the request path don't need to set it.
        self._base_url = base_url.rstrip("/")
        self._auth_provider = auth_provider
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
        self._debug_enabled = _truthy(os.getenv("MCP_LOG_DEBUG_ENABLED", "false"))

    @property
    def base_url(self) -> str:
        return self._base_url

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request, retrying on 429 / 502 / 503 / 504. Returns the raw response."""
        url = self._join_url(path)
        merged_headers = await self._merge_headers(headers)
        return await self._request_with_retry(
            method=method,
            url=url,
            params=params,
            json_body=json,
            headers=merged_headers,
        )

    # ------------------------------------------------------------------
    # Internal — retry loop + single send
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: Any | None,
        headers: dict[str, str],
    ) -> httpx.Response:
        attempt = 0
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                if self._debug_enabled:
                    _log_request(method, url, headers)

                try:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                except RETRYABLE_TRANSPORT_EXCEPTIONS as exc:
                    if attempt >= self._max_retries:
                        raise
                    delay = _compute_backoff(attempt)
                    attempt += 1
                    logger.info(
                        "Retrying %s %s after %.2fs (attempt %d/%d, transport=%s)",
                        method,
                        redact_url(url),
                        delay,
                        attempt,
                        self._max_retries,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(delay)
                    continue

                if self._debug_enabled:
                    _log_response(response)

                if response.status_code not in RETRYABLE_STATUSES:
                    return response
                if attempt >= self._max_retries:
                    return response

                delay = _compute_backoff(attempt, response)
                attempt += 1
                logger.info(
                    "Retrying %s %s after %.2fs (attempt %d/%d, status=%d)",
                    method,
                    redact_url(url),
                    delay,
                    attempt,
                    self._max_retries,
                    response.status_code,
                )
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Internal — header merging + URL building
    # ------------------------------------------------------------------

    async def _merge_headers(self, request_headers: dict[str, str] | None) -> dict[str, str]:
        """Compose the final outgoing header dict with documented precedence."""
        merged: dict[str, str] = {}
        if self._auth_provider is not None:
            try:
                provided = await self._auth_provider()
                if provided:
                    merged.update(provided)
            except Exception:
                # An auth_provider failure must NOT leak credentials via stderr.
                # Surface as an empty header set; the upstream API will return 401
                # which the handlers layer maps to AUTH_REQUIRED.
                logger.warning("auth_provider raised; continuing without injected headers")
        if request_headers:
            merged.update(request_headers)
        return merged

    def _join_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"


class GraphQLClient:
    """Async GraphQL client built on top of RestClient.

    POSTs `{query, variables, operationName}` JSON to a fixed URL and returns
    the raw httpx.Response. Response parsing (including partial-success
    handling) lives in `src/gateway/handlers.normalize_graphql_response`.
    """

    def __init__(
        self,
        *,
        url: str,
        auth_provider: AuthProvider | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._url = url
        # No base_url: the GraphQL endpoint URL is always absolute and is passed
        # as the request `path` (RestClient._join_url preserves absolute URLs).
        self._client = RestClient(
            auth_provider=auth_provider,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    @property
    def url(self) -> str:
        return self._url

    async def execute(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        if operation_name is not None:
            body["operationName"] = operation_name
        return await self._client.request("POST", self._url, json=body, headers=headers)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _compute_backoff(attempt: int, response: httpx.Response | None = None) -> float:
    """Compute backoff delay. Honors `Retry-After` for 429 if present and parseable.

    `response=None` is used for transport-level retries (no HTTP response received).
    """
    if response is not None and response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                # RFC 7231 also allows HTTP-date; here we accept seconds only.
                return min(float(retry_after), BACKOFF_CAP_SEC)
            except ValueError:
                pass
    base = BACKOFF_BASE_SEC * (2**attempt)
    jitter = random.random() * BACKOFF_BASE_SEC
    return min(base + jitter, BACKOFF_CAP_SEC)


def _log_request(method: str, url: str, headers: dict[str, str]) -> None:
    """DEBUG-level request log. Always passes headers through redaction."""
    logger.debug(
        "→ %s %s headers=%s",
        method,
        redact_url(url),
        redact_headers(headers),
    )


def _log_response(response: httpx.Response) -> None:
    """DEBUG-level response log. Headers redacted; body NEVER logged here."""
    logger.debug(
        "← %d %s headers=%s",
        response.status_code,
        redact_url(str(response.request.url)) if response.request else "",
        redact_headers(dict(response.headers)),
    )


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
