from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from src.auth import AuthError, AuthRequiredError, Credentials
from src.config import ApiConfig
from src.oauth_provider.request_context import get_current_mcp_user
from src.oauth_provider.schemas import SessionInfo

if TYPE_CHECKING:
    from src.tools.context import ToolContext

# Authoritative list of auth.type values the gateway handles. Both
# resolve_auth_headers and peek_auth_state branch on the early
# `auth_type not in KNOWN_AUTH_TYPES` check — so a new type that's added to
# this set but missing a branch in EITHER function will fall through to the
# raise/return-unknown line below, surfacing the gap immediately rather than
# silently mis-routing requests.
KNOWN_AUTH_TYPES: frozenset[str] = frozenset({"oauth2", "bearer", "api_key", "session_login"})


async def ensure_service_session(*, config: ApiConfig, context: ToolContext) -> SessionInfo | None:
    """Resolve the per-user Service API session for the current request.

    Resolution order:

    1. ``context.service_session`` — pre-injected (tests, future direct
       wiring). If set, use as-is.
    2. The current request's ``user_id`` (from the OAuth middleware's
       contextvar) plus ``context.service_session_store`` — look up and
       transparently refresh if near expiry.
    3. None — caller must surface AUTH_REQUIRED.

    Returns ``None`` only when the API doesn't use ``session_login`` OR
    no user / store is available. The caller (``resolve_auth_headers``)
    decides whether ``None`` is fatal based on the auth type.
    """
    if config.auth is None or (config.auth.type or "").lower() != "session_login":
        return None

    if context.service_session is not None:
        return context.service_session

    if context.service_session_store is None:
        return None

    user = get_current_mcp_user()
    if user is None:
        return None
    user_id = user.get("user_id")
    if not user_id:
        return None

    try:
        return await context.service_session_store.get(user_id)
    except (AuthRequiredError, AuthError):
        # Surface as "no session" — the auth resolver will raise
        # AuthRequiredError with the right operator-facing message.
        return None


class UnknownAuthTypeError(ValueError):
    """`auth.type` in api_configs.json is not one we know how to handle."""


async def resolve_auth_headers(
    *,
    config: ApiConfig,
    api_id: str,
    credentials: Credentials,
    service_session: SessionInfo | None = None,
) -> dict[str, str]:
    """Compute the auth headers for an outbound request based on `config.auth.type`.

    Returns the headers dict (possibly empty for no-auth APIs). Raises
    `AuthRequiredError` when a token / API key is missing for the configured
    auth type, and `UnknownAuthTypeError` when `auth.type` is not recognized.
    """
    auth = config.auth
    if auth is None:
        return {}

    auth_type = (auth.type or "").lower()
    if auth_type not in KNOWN_AUTH_TYPES:
        raise UnknownAuthTypeError(f"Unknown auth.type '{auth.type}' for '{api_id}'")

    if auth_type == "oauth2":
        # Credentials.get may raise AuthRequiredError; let it propagate.
        token = await credentials.get(api_id)
        if token is None:  # required=True default — should already have raised
            raise AuthRequiredError(f"No credentials available for '{api_id}'")
        return {"Authorization": f"Bearer {token.access_token}"}

    if auth_type == "bearer":
        if not auth.token_env:
            raise UnknownAuthTypeError(f"Bearer auth for '{api_id}' missing 'token_env' in config")
        token_value = os.environ.get(auth.token_env)
        if not token_value:
            raise AuthRequiredError(
                f"Bearer token env var '{auth.token_env}' not set for '{api_id}'"
            )
        return {"Authorization": f"Bearer {token_value}"}

    if auth_type == "api_key":
        if not auth.key_env or not auth.header_name:
            raise UnknownAuthTypeError(
                f"api_key auth for '{api_id}' missing 'key_env' or 'header_name'"
            )
        key_value = os.environ.get(auth.key_env)
        if not key_value:
            raise AuthRequiredError(f"API key env var '{auth.key_env}' not set for '{api_id}'")
        return {auth.header_name: key_value}

    if auth_type == "session_login":
        # Phase 9: per-user Service API session. The session is resolved
        # upstream (by the OAuth-aware middleware → ToolContext) and
        # threaded in via `service_session`. Without it we cannot speak
        # to the Service API on behalf of any user.
        if service_session is None:
            raise AuthRequiredError(
                f"Service API session required for '{api_id}'. The OAuth flow must "
                "complete (POST to /authorize/consent) before tools can call this API."
            )
        header_name = auth.session_header or "X-Session-Id"
        template = auth.session_format or "{session_id}"
        return {header_name: template.format(session_id=service_session.session_id)}

    # Unreachable: KNOWN_AUTH_TYPES check at the top covers all branches.
    raise UnknownAuthTypeError(
        f"Internal error: auth.type '{auth.type}' is in KNOWN_AUTH_TYPES "
        f"but no branch handled it. Add a case here when adding a new auth type."
    )


async def peek_auth_state(
    *, config: ApiConfig, api_id: str, credentials: Credentials
) -> tuple[str, float | None]:
    """Return (auth_state, expires_at_or_None) for `get_status` — strictly read-only.

    Mirrors the auth.type branches in `resolve_auth_headers` but uses
    `Credentials.peek()` for oauth2 (NEVER refreshes, NEVER opens a browser)
    and only checks env-var presence for bearer / api_key.

    Returned auth_state ∈ {"authenticated", "expired", "unauthenticated",
    "not_required", "unknown"}. `expires_at` is set only when an OAuth
    TokenInfo is present.
    """
    auth = config.auth
    if auth is None:
        return "not_required", None

    auth_type = (auth.type or "").lower()
    if auth_type not in KNOWN_AUTH_TYPES:
        return "unknown", None

    if auth_type == "oauth2":
        token = await credentials.peek(api_id)
        if token is None:
            return "unauthenticated", None
        if token.expires_at - time.time() < 0:
            return "expired", token.expires_at
        return "authenticated", token.expires_at

    if auth_type == "bearer":
        if auth.token_env and os.environ.get(auth.token_env):
            return "authenticated", None
        return "unauthenticated", None

    if auth_type == "api_key":
        if auth.key_env and os.environ.get(auth.key_env):
            return "authenticated", None
        return "unauthenticated", None

    if auth_type == "session_login":
        # `peek_auth_state` is read-only and per-API; it has no access
        # to the request's ToolContext. So the most we can say without
        # leaking per-user state into get_status is "this API is OAuth-
        # backed at the transport layer".
        return "oauth_managed", None

    # Unreachable: KNOWN_AUTH_TYPES check at the top covers all branches.
    return "unknown", None
