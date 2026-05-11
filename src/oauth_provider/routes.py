"""OAuth Provider ASGI dispatcher.

Builds the small handler-routing layer that backs every OAuth endpoint
plus the well-known discovery documents. The transport layer
(:mod:`src.transport.http`) calls :func:`build_oauth_dispatcher` to get
a single ASGI callable that owns these paths.

Routing table:

  GET    /.well-known/oauth-authorization-server   discovery (AS)
  GET    /.well-known/oauth-protected-resource     discovery (PRM)
  POST   /register                                 dynamic client registration
  GET    /authorize                                consent form
  POST   /authorize/consent                        consent processing
  POST   /token                                    token issue / refresh

Anything not in this table returns ``HANDLED == False`` from
:func:`try_handle`, so the outer dispatcher in
:mod:`src.transport.http` can route the request to the MCP session
manager (``/mcp``) or the not-found responder.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.types import Receive, Scope, Send

from src.config import ApiConfig

from .authorize import authorize_get_handler, consent_post_handler
from .discovery import (
    WELL_KNOWN_AS_PATH,
    discovery_handler,
    is_protected_resource_path,
)
from .register import register_handler
from .service_session import ServiceAuthCallable, ServiceSessionStore
from .store import OAuthStore
from .token import token_handler

Handler = Callable[[Scope, Receive, Send], Awaitable[None]]


def build_oauth_dispatcher(
    *,
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    authenticate: ServiceAuthCallable,
    api_config: ApiConfig,
    issuer: str,
) -> Callable[[Scope, Receive, Send], Awaitable[bool]]:
    """Return a dispatcher that handles every OAuth path.

    The returned callable resolves to ``True`` when it handled the
    request (response already sent) and ``False`` when the path is not
    one of ours — the caller should route the request elsewhere.
    """

    async def dispatcher(scope: Scope, receive: Receive, send: Send) -> bool:
        if scope.get("type") != "http":
            return False

        path = str(scope.get("path", ""))

        if path == WELL_KNOWN_AS_PATH or is_protected_resource_path(path):
            await discovery_handler(scope=scope, send=send, issuer=issuer)
            return True

        if path == "/register":
            await register_handler(scope=scope, receive=receive, send=send, store=store)
            return True

        if path == "/authorize":
            await authorize_get_handler(scope=scope, receive=receive, send=send, store=store)
            return True

        if path == "/authorize/consent":
            await consent_post_handler(
                scope=scope,
                receive=receive,
                send=send,
                store=store,
                service_session_store=service_session_store,
                authenticate=authenticate,
                api_config=api_config,
            )
            return True

        if path == "/token":
            await token_handler(scope=scope, receive=receive, send=send, store=store)
            return True

        return False

    return dispatcher
