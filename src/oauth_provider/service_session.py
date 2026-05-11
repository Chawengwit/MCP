"""Per-user Service API session manager with refresh-near-expiry.

Mirrors :class:`src.auth.credentials.Credentials` line-for-line:

- Per-``user_id`` :class:`asyncio.Lock` so concurrent tool calls do not
  each hit the Service API ``/auth`` endpoint when a single session is
  expired. The first coroutine refreshes; the rest wait and read.
- ``REFRESH_BUFFER_SEC`` head-room — refresh when less than 60 seconds
  remain, not exactly at expiry, to prevent the inevitable clock-skew
  race that hands a freshly-issued token an "expired" error.

Storage: encrypted rows in SQLite via
:class:`src.oauth_provider.store.OAuthStore`. ``api_key`` and
``secret_key`` are Fernet ciphertext; the plaintext only lives in memory
during the consent POST and the refresh call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from src.auth.credentials import AuthError, AuthRequiredError
from src.config import ApiConfig

from .encryption import DecryptionError, Encryptor
from .schemas import ServiceSession, SessionInfo
from .store import OAuthStore, _to_epoch, _utc_now

logger = logging.getLogger("mcp.oauth_provider.service_session")

REFRESH_BUFFER_SEC = 60.0  # refresh when < 60s remain — service sessions are short-lived


class ServiceAuthCallable(Protocol):
    """Signature of ``src.auth.service_api.authenticate``.

    Wrapped in a Protocol so the store can be unit-tested with a stub.
    """

    async def __call__(
        self,
        config: ApiConfig,
        *,
        api_key: str,
        secret_key: str,
    ) -> SessionInfo: ...


class ServiceSessionStore:
    """Concurrency-safe accessor over encrypted Service API sessions.

    Construct one per server process; the per-``user_id`` :class:`asyncio.Lock`
    map lives on the instance.
    """

    def __init__(
        self,
        *,
        store: OAuthStore,
        encryptor: Encryptor,
        api_config: ApiConfig,
        authenticate: ServiceAuthCallable,
    ) -> None:
        self._store = store
        self._encryptor = encryptor
        self._api_config = api_config
        self._authenticate = authenticate
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def save(
        self,
        *,
        user_id: str,
        api_key: str,
        secret_key: str,
        session: SessionInfo,
    ) -> None:
        """Persist a fresh login result (called by the consent handler).

        Both ``api_key`` and ``secret_key`` are encrypted before they
        hit the disk row. ``session.session_id`` is stored in the
        clear (it is itself a short-lived bearer token), and the
        redaction layer keeps it out of logs.
        """
        record = ServiceSession(
            user_id=user_id,
            company_group=session.company_group,
            encrypted_api_key=self._encryptor.encrypt(api_key),
            encrypted_secret_key=self._encryptor.encrypt(secret_key),
            session_id=session.session_id,
            session_expire=session.session_expire,
            user_type=session.user_type,
            app_package=session.app_package,
            updated_at=_utc_now(),
        )
        await self._store.upsert_service_session(record)

    async def get(self, user_id: str) -> SessionInfo:
        """Return a usable session, refreshing if near expiry.

        Raises:
          - AuthRequiredError: no session stored for this user (consent never given,
            or the user was deleted by an operator).
          - AuthError: Service API refresh failed (e.g. credentials rotated upstream).
        """
        async with self._lock_for(user_id):
            stored = await self._store.get_service_session(user_id)
            if stored is None:
                raise AuthRequiredError(
                    f"No Service API session stored for user '{user_id}'. "
                    "Re-run the OAuth consent flow."
                )
            if self._needs_refresh(stored):
                return await self._refresh(stored)
            return _to_session_info(stored)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        # Safe under asyncio: this method has no ``await`` between
        # dict-get and dict-set, so two concurrent coroutines cannot
        # interleave here. Do NOT add ``await`` inside this function — it
        # would open a real race window in which two callers each create
        # a fresh lock for the same user_id, defeating the whole point.
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    @staticmethod
    def _needs_refresh(stored: ServiceSession) -> bool:
        return (stored.session_expire - time.time()) < REFRESH_BUFFER_SEC

    async def _refresh(self, stored: ServiceSession) -> SessionInfo:
        """Re-authenticate against the Service API and persist the new session."""
        try:
            api_key = self._encryptor.decrypt(stored.encrypted_api_key)
            secret_key = self._encryptor.decrypt(stored.encrypted_secret_key)
        except DecryptionError as exc:
            # Almost always means the operator rotated MCP_OAUTH_ENCRYPTION_KEY
            # without clearing the table. Treat as "must re-consent".
            logger.warning(
                "Cannot decrypt stored credentials for user %s; consent required.",
                stored.user_id,
            )
            raise AuthRequiredError(
                f"Stored credentials for user '{stored.user_id}' are unreadable; "
                "re-run the OAuth consent flow."
            ) from exc

        try:
            session = await self._authenticate(
                self._api_config, api_key=api_key, secret_key=secret_key
            )
        except AuthError:
            raise
        except Exception as exc:
            # Specific subclass to keep callers off bare Exception. The
            # detail is intentionally generic — it is rendered into an
            # MCP error response and must not leak Service API internals.
            raise AuthError(
                f"Service API refresh failed for user '{stored.user_id}': {type(exc).__name__}"
            ) from exc

        # Persist the new session_id + expiry under the same encrypted
        # credentials — the api_key/secret_key are unchanged, so we keep
        # the existing ciphertext columns.
        new_record = ServiceSession(
            user_id=stored.user_id,
            company_group=session.company_group or stored.company_group,
            encrypted_api_key=stored.encrypted_api_key,
            encrypted_secret_key=stored.encrypted_secret_key,
            session_id=session.session_id,
            session_expire=session.session_expire,
            user_type=session.user_type or stored.user_type,
            app_package=session.app_package or stored.app_package,
            updated_at=_utc_now(),
        )
        await self._store.upsert_service_session(new_record)
        return session


def _to_session_info(stored: ServiceSession) -> SessionInfo:
    return SessionInfo(
        user_id=stored.user_id,
        session_id=stored.session_id,
        session_expire=stored.session_expire,
        company_group=stored.company_group,
        user_type=stored.user_type,
        app_package=stored.app_package,
    )


# Re-export for tests that need to construct rows with known expiry.
__all__ = [
    "REFRESH_BUFFER_SEC",
    "ServiceAuthCallable",
    "ServiceSessionStore",
    "_to_epoch",
]
