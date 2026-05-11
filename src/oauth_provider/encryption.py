"""Fernet symmetric encryption for at-rest Service API credentials.

The OAuth Provider stores per-user `api_key` and `secret_key` values
encrypted in SQLite. Encryption is symmetric (Fernet) because the same
process that writes (consent handler) also reads (gateway call site).

Key sourcing:
  - Read from ``MCP_OAUTH_ENCRYPTION_KEY`` env var at construction.
  - Must be 32 url-safe base64-encoded bytes (the Fernet wire format).
    Generate via::

        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

  - Missing key when OAuth is enabled raises ``ConfigurationError`` —
    fail-loud at startup rather than silently writing plaintext.

Rotation is out of scope for Phase 9: changing the env var invalidates
all stored sessions (decrypt raises ``InvalidToken``) — callers should
treat that as "all users must re-consent" and clear the table.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "MCP_OAUTH_ENCRYPTION_KEY"


class ConfigurationError(RuntimeError):
    """Raised when required OAuth Provider configuration is missing."""


class DecryptionError(RuntimeError):
    """Raised when ciphertext cannot be decrypted (corruption or wrong key)."""


class Encryptor:
    """Fernet wrapper that hides the key handling from callers.

    Instantiate once at server startup via :meth:`from_env` and pass into
    :class:`src.oauth_provider.store.OAuthStore` and
    :class:`src.oauth_provider.service_session.ServiceSessionStore`.
    """

    def __init__(self, key: bytes) -> None:
        # Fernet() validates the key shape — raises ValueError if not 32
        # url-safe base64 bytes. We re-raise as ConfigurationError so the
        # startup path uses one exception class.
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                f"{ENV_KEY} is malformed: must be 32 url-safe base64 bytes."
            ) from exc

    @classmethod
    def from_env(cls) -> Encryptor:
        raw = os.getenv(ENV_KEY, "").strip()
        if not raw:
            raise ConfigurationError(
                f"{ENV_KEY} is required when the OAuth Provider is enabled. "
                "Generate one with "
                '`python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"` '
                "and set it in your environment (never commit it)."
            )
        return cls(raw.encode("ascii"))

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a UTF-8 string. Returns Fernet token bytes."""
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt a Fernet token. Raises DecryptionError on failure.

        We deliberately do NOT echo the ciphertext or the key into the
        error message — the caller may log the exception type, but the
        plaintext stays private.
        """
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise DecryptionError(
                "Failed to decrypt stored credential — key mismatch or ciphertext corruption."
            ) from exc


def is_oauth_provider_enabled() -> bool:
    """True iff ``MCP_OAUTH_ENCRYPTION_KEY`` is set (non-empty).

    This is the canonical "is OAuth on" check used by the transport layer
    to decide whether to relax the loopback guard.
    """
    return bool(os.getenv(ENV_KEY, "").strip())
