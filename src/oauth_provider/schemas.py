"""Pydantic v2 schemas for OAuth Provider records (Phase 9).

Mirrors :mod:`src.events.schemas`: ``BaseModel`` + ``model_config =
{"extra": "forbid"}`` on every model so unknown keys are a Pydantic
validation error rather than silently dropped.

All timestamps are ``datetime`` in UTC — the storage layer converts to
epoch seconds for SQLite rows, but the in-memory representation stays
timezone-aware so comparisons with ``datetime.now(timezone.utc)`` are
unambiguous.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Base(BaseModel):
    """Shared `extra=forbid` config."""

    model_config = ConfigDict(extra="forbid")


class ClientRegistration(_Base):
    """OAuth 2.0 Dynamic Client Registration record (RFC 7591).

    Public client only: no ``client_secret``. Issued for the lifetime of
    the row; revocation is delete-row.
    """

    client_id: str
    client_name: str
    redirect_uris: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)


class AuthorizationCode(_Base):
    """Single-use authorization code issued at ``/authorize/consent``.

    Bound to one ``redirect_uri`` and one PKCE challenge — both must
    match at ``/token`` exchange or the row is rejected.
    """

    code: str
    client_id: str
    user_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: Literal["S256"]
    expires_at: datetime
    created_at: datetime = Field(default_factory=_utc_now)


class AccessToken(_Base):
    """Opaque per-user access token issued at ``/token``.

    Maps to a Service API session via :class:`ServiceSession.user_id`.
    Revocation = delete the row.
    """

    token: str
    client_id: str
    user_id: str
    expires_at: datetime
    refresh_token: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    last_used_at: datetime = Field(default_factory=_utc_now)


class ServiceSession(_Base):
    """Encrypted per-user Service API session.

    ``encrypted_api_key`` / ``encrypted_secret_key`` are Fernet ciphertext
    produced by :class:`src.oauth_provider.encryption.Encryptor`.
    ``session_id`` is the raw Service API session token used at request
    time — kept in memory only when needed; the on-disk row carries it
    in the clear because revocation depends on it. The Service API
    treats session_id as a short-lived bearer.

    NOTE: ``session_id`` MUST NOT appear in any log line. Redaction in
    :mod:`src.events.redaction` covers this for serialised payloads.
    """

    user_id: str
    company_group: str | None = None
    encrypted_api_key: bytes
    encrypted_secret_key: bytes
    session_id: str
    session_expire: int  # epoch seconds
    user_type: str | None = None
    app_package: str | None = None
    updated_at: datetime = Field(default_factory=_utc_now)


class SessionInfo(_Base):
    """View of a Service API authentication response.

    Returned by :func:`src.auth.service_api.authenticate`. Includes the
    raw ``session_id`` for immediate use; callers that serialise the
    model for logging MUST pass ``exclude={"session_id"}`` to
    ``model_dump`` — or, better, route the output through
    :func:`src.events.redaction.redact_body`.
    """

    user_id: str
    session_id: str
    session_expire: int  # epoch seconds (absolute, not duration)
    company_group: str | None = None
    user_type: str | None = None
    app_package: str | None = None


class ConsentForm(_Base):
    """Body of ``POST /authorize/consent``.

    The query-string params (``client_id``, ``redirect_uri``, ``state``,
    ``code_challenge``, ``code_challenge_method``, ``response_type``) are
    carried as hidden fields in the consent HTML; the user supplies
    ``api_key`` and ``secret_key``.
    """

    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"]
    response_type: Literal["code"]
    api_key: str
    secret_key: str
