"""OAuth 2.0 Authorization Server for the MCP Data Gateway (Phase 9).

Exposes per-user access tokens to Claude.ai's "Add custom connector"
flow, mapping each token to an encrypted Service API session in SQLite.

Mirror this package on :mod:`src.events`:

- Public classes (:class:`OAuthStore`, :class:`ServiceSessionStore`,
  :class:`OAuthProvider`) with ``from_env()`` classmethods.
- Async-only public methods; sqlite3 work is offloaded via
  ``asyncio.to_thread``.
- All sensitive fields redacted in :mod:`src.events.redaction`.

The transport layer (:mod:`src.transport.http`) composes the OAuth
dispatcher and middleware on top of the existing Phase 8 ASGI app —
when ``MCP_OAUTH_ENCRYPTION_KEY`` is unset the OAuth surface is not
mounted and the static-bearer Phase 8 path is unchanged.
"""

from __future__ import annotations

from .encryption import (
    ConfigurationError,
    DecryptionError,
    Encryptor,
    is_oauth_provider_enabled,
)
from .schemas import (
    AccessToken,
    AuthorizationCode,
    ClientRegistration,
    ConsentForm,
    ServiceSession,
    SessionInfo,
)
from .service_session import ServiceSessionStore
from .store import OAuthStore

__all__ = [
    "AccessToken",
    "AuthorizationCode",
    "ClientRegistration",
    "ConfigurationError",
    "ConsentForm",
    "DecryptionError",
    "Encryptor",
    "OAuthStore",
    "ServiceSession",
    "ServiceSessionStore",
    "SessionInfo",
    "is_oauth_provider_enabled",
]
