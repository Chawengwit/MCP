"""Tests for the MCP_TRANSPORT dispatcher in src.server.main().

Strategy: monkeypatch `_serve_stdio` / `_serve_http` (and the upstream
constructors) so we can assert the dispatcher branches correctly without
actually opening sockets or stdio streams.
"""

from __future__ import annotations

from typing import Any

import pytest
from src import server as server_module


class _SentinelError(Exception):
    """Raised by the patched serve fns to signal which branch ran."""


@pytest.fixture
def stub_main_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the heavy parts of main() so the dispatcher is exercised in isolation.

    Returns a dict of mutable counters / captured values for assertion.
    """
    captured: dict[str, Any] = {"recorder_stops": 0}

    monkeypatch.setattr(server_module, "load_api_configs", lambda _path: {})

    class _StubRecorder:
        async def start(self) -> None:  # noqa: D401 — short stub
            pass

        async def stop(self) -> None:
            captured["recorder_stops"] += 1

    monkeypatch.setattr(
        server_module.Recorder, "from_env", classmethod(lambda cls: _StubRecorder())
    )

    # Build a no-op ToolContext / Server — main() doesn't actually use them when
    # the serve fns are stubbed.
    monkeypatch.setattr(
        server_module, "_build_tool_context", lambda *, api_configs, recorder: object()
    )
    monkeypatch.setattr(server_module, "_build_registry", lambda _ctx: _StubRegistry())
    monkeypatch.setattr(server_module, "_build_server", lambda _registry: object())

    return captured


class _StubRegistry:
    def all(self) -> dict[str, object]:
        return {}


async def test_default_transport_is_stdio(
    monkeypatch: pytest.MonkeyPatch, stub_main_dependencies: dict[str, Any]
) -> None:
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)

    async def _stdio_branch(_server: Any) -> None:
        raise _SentinelError("stdio")

    async def _http_branch(_server: Any) -> None:
        raise _SentinelError("http")

    monkeypatch.setattr(server_module, "_serve_stdio", _stdio_branch)
    monkeypatch.setattr(server_module, "_serve_http", _http_branch)

    with pytest.raises(_SentinelError, match="stdio"):
        await server_module.main()
    assert stub_main_dependencies["recorder_stops"] == 1


async def test_http_transport_routes_to_http_branch(
    monkeypatch: pytest.MonkeyPatch, stub_main_dependencies: dict[str, Any]
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")

    async def _stdio_branch(_server: Any) -> None:
        raise _SentinelError("stdio")

    async def _http_branch(_server: Any, _api_configs: Any) -> None:
        raise _SentinelError("http")

    monkeypatch.setattr(server_module, "_serve_stdio", _stdio_branch)
    monkeypatch.setattr(server_module, "_serve_http", _http_branch)

    with pytest.raises(_SentinelError, match="http"):
        await server_module.main()
    assert stub_main_dependencies["recorder_stops"] == 1


async def test_unknown_transport_exits_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
    stub_main_dependencies: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "websocket")

    with pytest.raises(SystemExit) as exc_info:
        await server_module.main()
    assert exc_info.value.code == 1

    err = capsys.readouterr().err
    assert "Unknown MCP_TRANSPORT='websocket'" in err
    assert "stdio" in err and "http" in err
    # Recorder.stop() must still run even on exit path.
    assert stub_main_dependencies["recorder_stops"] == 1


async def test_transport_value_is_lowercased_and_stripped(
    monkeypatch: pytest.MonkeyPatch, stub_main_dependencies: dict[str, Any]
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "  HTTP  ")

    async def _stdio_branch(_server: Any) -> None:
        raise _SentinelError("stdio")

    async def _http_branch(_server: Any, _api_configs: Any) -> None:
        raise _SentinelError("http")

    monkeypatch.setattr(server_module, "_serve_stdio", _stdio_branch)
    monkeypatch.setattr(server_module, "_serve_http", _http_branch)

    with pytest.raises(_SentinelError, match="http"):
        await server_module.main()
