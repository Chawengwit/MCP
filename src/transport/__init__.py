"""Transport layer for the MCP Data Gateway.

Two transports are supported, selected by the `MCP_TRANSPORT` env var:

  - `stdio` (default) — `run_stdio` from :mod:`src.transport.stdio`
  - `http`            — `run_http`  from :mod:`src.transport.http`

`src.server.main()` constructs a single :class:`mcp.server.Server` instance and
dispatches to one of these runners. Tool handlers are transport-agnostic — only
the wire format around them changes.
"""

from __future__ import annotations

from .http import (
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
from .stdio import run_stdio

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "LOOPBACK_HOSTS",
    "LoopbackGuardError",
    "bearer_auth_middleware",
    "build_app",
    "check_loopback_guard",
    "resolve_http_settings",
    "run_http",
    "run_stdio",
]
