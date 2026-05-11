"""Per-request OAuth user context, propagated via :mod:`contextvars`.

The OAuth middleware resolves a Bearer token to a ``user_id`` /
``auth_source`` / ``client_id`` triple at the ASGI layer. Tool handlers
running deeper in the MCP SDK can't see ASGI ``scope`` directly, so we
publish the resolved identity into a ``ContextVar`` set on every HTTP
request. Tool code reads it back via :func:`get_current_mcp_user`.

contextvars are the right vehicle here because:

- They are async-task-local — the right asyncio task sees the right user
  even under concurrent tool calls.
- They reset automatically when the task ends — no manual cleanup.
- They survive ``await`` boundaries within the same task (the dispatcher
  hands the request to the MCP SDK with an ``await``; the var stays set).

This module is intentionally tiny and dependency-free so both the
middleware and the tool layer can import it without cycles.
"""

from __future__ import annotations

import contextvars
from typing import TypedDict


class McpUser(TypedDict):
    """Resolved per-request identity attached by the OAuth middleware."""

    user_id: str | None
    """User identifier from the OAuth Provider store (``None`` for static-bearer)."""

    auth_source: str
    """``"oauth"`` or ``"static_bearer"``."""

    client_id: str | None
    """Registered OAuth client (``None`` for static-bearer)."""


_current_mcp_user: contextvars.ContextVar[McpUser | None] = contextvars.ContextVar(
    "mcp_user", default=None
)


def set_current_mcp_user(user: McpUser | None) -> contextvars.Token[McpUser | None]:
    """Set the user for the current asyncio task.

    Returns a ``Token`` that callers may pass to :meth:`ContextVar.reset`
    to restore the previous value, mirroring the standard contextvars
    pattern. The middleware typically does not call ``reset`` — the var
    is naturally garbage-collected when the task ends.
    """
    return _current_mcp_user.set(user)


def get_current_mcp_user() -> McpUser | None:
    """Return the user resolved by the middleware for this request.

    ``None`` means either: (a) no Bearer token was presented (and the
    request reached a path that doesn't require one), or (b) we are not
    running inside an ASGI request at all (e.g. stdio mode, or a unit
    test that didn't set the var). Callers must handle both — do not
    treat the absence of a user as an authentication failure.
    """
    return _current_mcp_user.get()
