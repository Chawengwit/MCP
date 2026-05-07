from __future__ import annotations

import asyncio
import logging
import sys

import keyring
import keyring.errors

from .oauth import OAuth, OAuthConfig, TokenInfo

logger = logging.getLogger("mcp.auth.credentials")

DEFAULT_SERVICE_NAME = "mcp-data-gateway"
REFRESH_BUFFER_SEC = 300.0  # auto-refresh when < 5 minutes remain


class CredentialStorageError(RuntimeError):
    """Raised when the credential backend (keyring) is unavailable."""


class AuthError(RuntimeError):
    """Base for auth-related runtime errors surfaced to callers."""


class AuthRequiredError(AuthError):
    """No usable credentials are stored; the caller must trigger an OAuth flow."""


class Credentials:
    """Keyring-backed credential store for OAuth 2.0 tokens.

    Concurrency: a per-`api_id` `asyncio.Lock` prevents two refresh races for
    the same provider. The first caller refreshes; the rest wait and read the
    fresh value.

    Storage: tokens are serialized as the TokenInfo Pydantic JSON form and
    written to the system keyring under one service name (default
    "mcp-data-gateway"); the keyring username slot is the `api_id`.
    """

    def __init__(
        self,
        *,
        oauth: OAuth,
        oauth_configs: dict[str, OAuthConfig],
        service_name: str = DEFAULT_SERVICE_NAME,
    ) -> None:
        self._oauth = oauth
        self._oauth_configs = oauth_configs
        self._service_name = service_name
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, api_id: str, required: bool = True) -> TokenInfo | None:
        """Return a usable TokenInfo, refreshing if near expiry.

        Behavior:
          - No token stored: raise AuthRequiredError if required, else return None.
          - Token near expiry (< 5 min) AND refresh_token present: refresh in place.
          - Otherwise: return as-is.
        """
        async with self._lock_for(api_id):
            stored = self._read(api_id)
            if stored is None:
                if required:
                    raise AuthRequiredError(
                        f"No credentials stored for '{api_id}'. OAuth flow required."
                    )
                return None

            if self._needs_refresh(stored):
                if stored.refresh_token is None:
                    raise AuthRequiredError(
                        f"Credentials for '{api_id}' are expired and no refresh_token "
                        f"is available. Re-authentication required."
                    )
                refreshed = await self._refresh(api_id, stored.refresh_token)
                self._write(api_id, refreshed)
                return refreshed

            return stored

    async def peek(self, api_id: str) -> TokenInfo | None:
        """Return whatever is currently stored for `api_id`, with no side effects.

        Never refreshes, never triggers OAuth, never modifies storage. Used by
        Phase 5's `get_status` tool for read-only state inspection.
        """
        return self._read(api_id)

    async def store(self, api_id: str, tokens: TokenInfo) -> None:
        """Persist a TokenInfo for `api_id`."""
        self._write(api_id, tokens)

    async def clear(self, api_id: str) -> None:
        """Delete stored credentials for `api_id` (if any)."""
        try:
            keyring.delete_password(self._service_name, api_id)
        except keyring.errors.PasswordDeleteError:
            # No password stored — harmless.
            pass
        except keyring.errors.NoKeyringError as exc:
            raise _no_keyring_error() from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lock_for(self, api_id: str) -> asyncio.Lock:
        # Safe under asyncio: this method has no `await` between dict-get and
        # dict-set, so two concurrent coroutines cannot interleave here. Do NOT
        # add `await` inside this function — it would open a real race window
        # in which two callers each create a fresh lock for the same api_id.
        lock = self._locks.get(api_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[api_id] = lock
        return lock

    @staticmethod
    def _needs_refresh(token: TokenInfo) -> bool:
        import time as _time

        return token.expires_at - _time.time() < REFRESH_BUFFER_SEC

    def _read(self, api_id: str) -> TokenInfo | None:
        try:
            raw = keyring.get_password(self._service_name, api_id)
        except keyring.errors.NoKeyringError as exc:
            raise _no_keyring_error() from exc

        if raw is None:
            return None
        try:
            return TokenInfo.model_validate_json(raw)
        except Exception as exc:
            # Stored value is corrupt — treat as missing rather than crashing the tool.
            logger.warning("Stored credentials for %s are unreadable; ignoring.", api_id)
            _warn(f"unreadable credentials for {api_id}: {type(exc).__name__}")
            return None

    def _write(self, api_id: str, tokens: TokenInfo) -> None:
        # Pydantic v2 serializes to JSON without exposing secrets in repr/log.
        payload = tokens.model_dump_json()
        try:
            keyring.set_password(self._service_name, api_id, payload)
        except keyring.errors.NoKeyringError as exc:
            raise _no_keyring_error() from exc

    async def _refresh(self, api_id: str, refresh_token: str) -> TokenInfo:
        config = self._oauth_configs.get(api_id)
        if config is None:
            raise AuthRequiredError(f"No OAuth configuration for '{api_id}'; cannot refresh token.")
        return await self._oauth.refresh(config, refresh_token)


def _no_keyring_error() -> CredentialStorageError:
    return CredentialStorageError(
        "No keyring backend available. Install 'keyrings.alt' "
        "(pip install keyrings.alt) or set MCP_CREDENTIALS_STORAGE=file "
        "(file backend not yet implemented)."
    )


def _warn(msg: str) -> None:
    print(f"[mcp.auth.credentials] {msg}", file=sys.stderr)
