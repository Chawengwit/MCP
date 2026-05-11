"""Keyring-backed Service API session store for STDIO mode.

In Phase 9 the OAuth Provider stores per-user Service API sessions in an
encrypted SQLite table (the multi-tenant HTTP path). STDIO clients
(Claude Desktop) have no OAuth flow — there is one operator running the
server, so we keep the credentials + session in the OS keyring under the
same service-name convention as :mod:`src.auth.credentials`. One keyring
entry per ``api_id`` holds:

  - ``api_key`` + ``secret_key`` (the long-lived credentials that survive
    session expiry and let us silently refresh)
  - the current ``session_id`` + ``session_expire``
  - the cached metadata (``user_id``, ``company_group``, etc.) so we don't
    have to call the Service API on every request just to populate
    :class:`SessionInfo`.

Refresh behavior mirrors :class:`src.auth.credentials.Credentials`:

  - per-``api_id`` :class:`asyncio.Lock` prevents two concurrent tool
    calls from each hitting ``/auth`` when the session is near expiry
  - the ``no await`` invariant in :meth:`_lock_for` (mirrored verbatim
    from ``src/auth/credentials.py:113-122``) keeps the dict-of-locks
    race-free under asyncio

The operator populates this store by running
``python -m scripts.session_login <api_id>``. Tools then read via
:meth:`get` at request time.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Protocol

import keyring
from keyring.errors import NoKeyringError, PasswordDeleteError
from pydantic import BaseModel, ConfigDict

from src.auth.credentials import (
    DEFAULT_SERVICE_NAME,
    AuthError,
    AuthRequiredError,
    CredentialStorageError,
)
from src.config import ApiConfig
from src.oauth_provider.schemas import SessionInfo

from .service_api import authenticate as default_authenticate


class _Authenticator(Protocol):
    """Signature of :func:`src.auth.service_api.authenticate`.

    Wrapped in a Protocol so tests can pass a stub and mypy still
    type-checks calls inside :meth:`KeyringServiceSessionStore._refresh`.
    """

    async def __call__(
        self, config: ApiConfig, *, api_key: str, secret_key: str
    ) -> SessionInfo: ...


logger = logging.getLogger("mcp.auth.session_login_keyring")

# Refresh slightly earlier than the Service API's idea of expiry — clock
# skew is real, and 60 s of headroom is cheap. Mirrors the same buffer
# used in :class:`src.oauth_provider.service_session.ServiceSessionStore`.
REFRESH_BUFFER_SEC = 60.0


class StoredSession(BaseModel):
    """The single JSON blob persisted to a keyring slot.

    Pydantic v2 serializes / deserializes through ``model_dump_json`` /
    ``model_validate_json``. ``extra="forbid"`` prevents stray keys from
    silently surviving a schema change.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str
    secret_key: str
    user_id: str
    session_id: str
    session_expire: int
    company_group: str | None = None
    user_type: str | None = None
    app_package: str | None = None


class KeyringServiceSessionStore:
    """STDIO-mode session store backed by the OS keyring.

    The keyring slot name is the ``api_id`` from ``api_configs.json``;
    the service name defaults to ``mcp-data-gateway`` so this store
    coexists with :class:`src.auth.credentials.Credentials` (which keys
    different api_ids under the same service name without colliding).
    """

    def __init__(
        self,
        *,
        configs: dict[str, ApiConfig],
        service_name: str = DEFAULT_SERVICE_NAME,
        authenticate: _Authenticator = default_authenticate,
    ) -> None:
        self._configs = configs
        self._service_name = service_name
        # Stored as plain object so tests can pass a stub. The default is
        # ``service_api.authenticate`` so production paths never need to
        # think about wiring.
        self._authenticate = authenticate
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_configs(cls, configs: dict[str, ApiConfig]) -> KeyringServiceSessionStore:
        return cls(configs=configs)

    # ------------------------------------------------------------------
    # Public API used by the operator CLI + the auth resolver
    # ------------------------------------------------------------------

    async def save(
        self,
        *,
        api_id: str,
        api_key: str,
        secret_key: str,
        user_id: str,
        session_id: str,
        session_expire: int,
        company_group: str | None = None,
        user_type: str | None = None,
        app_package: str | None = None,
    ) -> None:
        """Persist a fresh login result to the keyring slot for ``api_id``."""
        record = StoredSession(
            api_key=api_key,
            secret_key=secret_key,
            user_id=user_id,
            session_id=session_id,
            session_expire=session_expire,
            company_group=company_group,
            user_type=user_type,
            app_package=app_package,
        )
        await asyncio.to_thread(self._write, api_id, record)

    async def clear(self, api_id: str) -> None:
        """Delete the keyring slot for ``api_id`` (no-op if empty)."""
        try:
            await asyncio.to_thread(keyring.delete_password, self._service_name, api_id)
        except PasswordDeleteError:
            return  # nothing stored — harmless
        except NoKeyringError as exc:  # pragma: no cover — env-dependent
            raise _no_keyring_error() from exc

    async def peek(self, api_id: str) -> StoredSession | None:
        """Return whatever is stored for ``api_id`` without refreshing.

        ``None`` means no entry. Used by ``get_status`` so the read-only
        introspection path does NOT trigger a Service API call.
        """
        return await asyncio.to_thread(self._read, api_id)

    async def get(self, api_id: str) -> StoredSession:
        """Return a usable session, refreshing if near expiry.

        Refresh chain:
          - If the stored session is still valid → return as-is.
          - Otherwise re-auth against the Service API with the stored
            api_key + secret_key, persist the new session, return it.

        Raises:
          - ``AuthRequiredError`` if no entry is stored (operator must
            run ``python -m scripts.session_login <api_id>``).
          - ``AuthError`` if the refresh itself fails (Service API down
            or credentials revoked — operator must intervene).
        """
        async with self._lock_for(api_id):
            stored = self._read(api_id)
            if stored is None:
                raise AuthRequiredError(
                    f"No Service API session stored for '{api_id}'. "
                    f"Run: python -m scripts.session_login {api_id}"
                )

            if self._needs_refresh(stored):
                return await self._refresh(api_id, stored)
            return stored

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lock_for(self, api_id: str) -> asyncio.Lock:
        # Safe under asyncio: this method has no ``await`` between
        # dict-get and dict-set, so two concurrent coroutines cannot
        # interleave here. Do NOT add ``await`` inside this function — it
        # would open a real race window in which two callers each create
        # a fresh lock for the same api_id, defeating the lock entirely.
        # (Mirrored verbatim from src/auth/credentials.py:113-122.)
        lock = self._locks.get(api_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[api_id] = lock
        return lock

    @staticmethod
    def _needs_refresh(session: StoredSession) -> bool:
        return (session.session_expire - time.time()) < REFRESH_BUFFER_SEC

    def _read(self, api_id: str) -> StoredSession | None:
        try:
            raw = keyring.get_password(self._service_name, api_id)
        except NoKeyringError as exc:  # pragma: no cover — env-dependent
            raise _no_keyring_error() from exc
        if raw is None:
            return None
        try:
            return StoredSession.model_validate_json(raw)
        except Exception as exc:
            # Stored payload is corrupt or from an old schema — treat as
            # missing rather than crashing the tool path.
            logger.warning("Stored session for %s is unreadable; ignoring.", api_id)
            _warn(f"unreadable session for {api_id}: {type(exc).__name__}")
            return None

    def _write(self, api_id: str, record: StoredSession) -> None:
        try:
            keyring.set_password(self._service_name, api_id, record.model_dump_json())
        except NoKeyringError as exc:  # pragma: no cover — env-dependent
            raise _no_keyring_error() from exc

    async def _refresh(self, api_id: str, stored: StoredSession) -> StoredSession:
        config = self._configs.get(api_id)
        if config is None:
            # The api_id was removed from the config after the user
            # logged in. Treat as "missing" — operator must clean up.
            raise AuthRequiredError(
                f"Stored session for '{api_id}' has no matching api config; "
                f"re-run: python -m scripts.session_login {api_id}"
            )

        try:
            session_info = await self._authenticate(
                config, api_key=stored.api_key, secret_key=stored.secret_key
            )
        except AuthError:
            # Service API has rejected the credentials (e.g. operator
            # rotated them in the Taximail dashboard). Bubble up so the
            # tool path returns AUTH_REQUIRED with the right CTA.
            raise
        except Exception as exc:
            # Wrap any other failure in AuthError so callers never see
            # bare exceptions.
            raise AuthError(
                f"Service API refresh failed for '{api_id}': {type(exc).__name__}"
            ) from exc

        refreshed = StoredSession(
            api_key=stored.api_key,
            secret_key=stored.secret_key,
            user_id=session_info.user_id,
            session_id=session_info.session_id,
            session_expire=session_info.session_expire,
            company_group=session_info.company_group or stored.company_group,
            user_type=session_info.user_type or stored.user_type,
            app_package=session_info.app_package or stored.app_package,
        )
        self._write(api_id, refreshed)
        return refreshed


def _no_keyring_error() -> CredentialStorageError:
    return CredentialStorageError(
        "No keyring backend available. Install 'keyrings.alt' "
        "(pip install keyrings.alt) — required for storing the Service "
        "API session in STDIO mode."
    )


def _warn(msg: str) -> None:
    print(f"[mcp.auth.session_login_keyring] {msg}", file=sys.stderr)
