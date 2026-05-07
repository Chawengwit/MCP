from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest


class MockTransport:
    """A minimal httpx transport that records requests and replays canned responses.

    Each call advances through the configured `responses` list. If the list runs
    out, the last entry is reused (handy for "always 200" tests). Each recorded
    request is appended to `requests` so tests can introspect headers, body, etc.
    """

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.responses: list[httpx.Response] = responses or []
        self.requests: list[httpx.Request] = []
        self._index = 0

    def queue(self, *responses: httpx.Response) -> None:
        self.responses.extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(200, request=request)
        idx = min(self._index, len(self.responses) - 1)
        self._index += 1
        response = self.responses[idx]
        # Re-bind to current request so .request is set on the response copy.
        return httpx.Response(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch) -> MockTransport:
    """Replace httpx.AsyncClient with one that uses a deterministic in-memory transport."""
    transport_holder = MockTransport()

    original_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(original_async_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(transport_holder)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("src.gateway.api_client.httpx.AsyncClient", _PatchedAsyncClient)
    return transport_holder


@pytest.fixture
def patch_async_client_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Install a custom handler function as the AsyncClient transport.

    Tests pass a handler taking an httpx.Request and returning httpx.Response.
    The handler may also raise httpx exceptions to simulate transport failures.
    Use this when MockTransport's queued-response model is too rigid (e.g. when
    the response depends on call count or the handler must raise a transport error).
    """
    original_async_client = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        class _PatchedAsyncClient(original_async_client):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr("src.gateway.api_client.httpx.AsyncClient", _PatchedAsyncClient)

    return install


@pytest.fixture
def mock_auth_provider() -> Callable[[], Awaitable[dict[str, str]]]:
    """auth_provider that always returns a fixed Bearer token."""

    async def provide() -> dict[str, str]:
        return {"Authorization": "Bearer auth_provider_token"}

    return provide


def json_response(
    status_code: int = 200,
    body: dict[str, Any] | list[Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Helper for building canned JSON responses in tests."""
    import json as _json

    payload = _json.dumps(body if body is not None else {}).encode("utf-8")
    final_headers = {"content-type": "application/json"}
    if headers:
        final_headers.update(headers)
    return httpx.Response(status_code=status_code, headers=final_headers, content=payload)


def text_response(
    status_code: int = 200,
    body: str = "",
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    final_headers = {"content-type": "text/plain; charset=utf-8"}
    if headers:
        final_headers.update(headers)
    return httpx.Response(status_code=status_code, headers=final_headers, content=body.encode())


def binary_response(
    status_code: int = 200,
    content: bytes = b"",
    *,
    content_type: str = "image/png",
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": content_type},
        content=content,
    )
