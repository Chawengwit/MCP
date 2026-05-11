from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from src.auth import Credentials
from src.config import ApiConfig
from src.events import Recorder
from src.gateway import GraphQLClient, RestClient
from src.oauth_provider.schemas import SessionInfo
from src.oauth_provider.service_session import ServiceSessionStore

AuthSource = Literal["oauth", "static_bearer"]


@dataclass
class ToolContext:
    """Dependency container passed to every Phase 5 tool handler.

    Built once at server startup; closures over this object hand each tool the
    config map, credential store, gateway factories, and Recorder it needs.
    Tests construct one with mock factories and a tmp-dir-backed Recorder.

    Phase 9 additions (all default to ``None`` so existing call sites keep
    working unchanged):

      - ``user_id`` — the Service API user resolved from the OAuth access
        token. ``None`` means the request came in over the Phase 8
        static-bearer path (no per-user identity).
      - ``service_session`` — the corresponding :class:`SessionInfo`. The
        ``session_id`` is short-lived; refresh is handled inside
        :class:`src.oauth_provider.service_session.ServiceSessionStore`.
      - ``auth_source`` — one of ``"oauth"`` or ``"static_bearer"`` so the
        gateway can pick the right header (``X-Session-Id`` for the
        Service API vs. ``Authorization: Bearer`` for everything else).
    """

    configs: dict[str, ApiConfig]
    credentials: Credentials
    rest_client_factory: Callable[[ApiConfig], RestClient]
    graphql_client_factory: Callable[[ApiConfig], GraphQLClient]
    recorder: Recorder
    user_id: str | None = None
    service_session: SessionInfo | None = None
    auth_source: AuthSource | None = None
    service_session_store: ServiceSessionStore | None = None
    """Phase 9.2 — per-request session resolver.

    When the OAuth middleware authenticates a request, it publishes the
    user_id to a contextvar. Tools that hit a ``session_login`` API call
    :func:`src.tools.auth_resolver.ensure_service_session` which reads the
    contextvar, then uses this store to fetch (and refresh) the
    per-user Service API session. ``None`` means the server is running
    without OAuth (stdio, static-bearer-only, or unit tests).
    """
