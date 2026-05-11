"""SQLite-backed OAuth Provider storage.

Holds:
  - Dynamically registered clients (RFC 7591, public clients only)
  - One-shot authorization codes (10-min TTL, single-use)
  - Opaque access tokens + optional refresh tokens
  - Encrypted Service API sessions per user_id (handled by
    :mod:`src.oauth_provider.service_session` — this module owns the
    raw row CRUD only)

Concurrency model: every public method does its sqlite3 work inside an
``asyncio.to_thread`` callable that opens a **fresh** connection. This
avoids the "SQLite objects can only be used in the thread that created
them" pitfall — connections are short-lived and thread-local by
construction. ``journal_mode=WAL`` is set on every connection so
concurrent readers do not block writers.

Mirror pattern:
  - Public class + ``from_env()`` classmethod — see
    :mod:`src.events.recorder` :func:`Recorder.from_env`.
  - All public methods async; never raise to the tool path on transient
    failures the caller can't act on.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schemas import (
    AccessToken,
    AuthorizationCode,
    ClientRegistration,
    ServiceSession,
)

DEFAULT_DB_PATH = Path("data/oauth_provider.db")
ENV_DB_PATH = "MCP_OAUTH_DB_PATH"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# Token sizes — chosen so collisions across the realistic install base are
# astronomically unlikely while staying URL-safe.
CLIENT_ID_BYTES = 24
ACCESS_TOKEN_BYTES = 48
REFRESH_TOKEN_BYTES = 48
AUTH_CODE_BYTES = 32


class OAuthStoreError(RuntimeError):
    """Raised on storage-layer failures the caller should surface to user."""


class OAuthStore:
    """Async facade over the OAuth Provider SQLite database.

    All datetime fields cross the boundary as timezone-aware UTC
    ``datetime`` objects on the Python side; SQLite rows hold the
    corresponding epoch-seconds INTEGER.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> OAuthStore:
        raw = os.getenv(ENV_DB_PATH, "").strip()
        db_path = Path(raw) if raw else DEFAULT_DB_PATH
        return cls(db_path)

    async def init_db(self) -> None:
        """Create tables idempotently. Safe to call multiple times."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        ddl = SCHEMA_FILE.read_text(encoding="utf-8")
        await asyncio.to_thread(self._init_db_sync, ddl)

    def _init_db_sync(self, ddl: str) -> None:
        with self._connect() as conn:
            conn.executescript(ddl)
            conn.commit()

    # ------------------------------------------------------------------
    # Clients (Dynamic Client Registration)
    # ------------------------------------------------------------------

    async def register_client(
        self,
        *,
        client_name: str,
        redirect_uris: list[str],
    ) -> ClientRegistration:
        client_id = _new_token(CLIENT_ID_BYTES)
        created_at = _utc_now()
        record = ClientRegistration(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=list(redirect_uris),
            created_at=created_at,
        )
        await asyncio.to_thread(self._insert_client_sync, record)
        return record

    def _insert_client_sync(self, record: ClientRegistration) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clients (client_id, client_name, redirect_uris, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    record.client_id,
                    record.client_name,
                    json.dumps(record.redirect_uris),
                    _to_epoch(record.created_at),
                ),
            )
            conn.commit()

    async def get_client(self, client_id: str) -> ClientRegistration | None:
        return await asyncio.to_thread(self._get_client_sync, client_id)

    def _get_client_sync(self, client_id: str) -> ClientRegistration | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_id, client_name, redirect_uris, created_at "
                "FROM clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return ClientRegistration(
            client_id=row[0],
            client_name=row[1],
            redirect_uris=json.loads(row[2]),
            created_at=_from_epoch(int(row[3])),
        )

    async def list_clients(self) -> list[ClientRegistration]:
        return await asyncio.to_thread(self._list_clients_sync)

    def _list_clients_sync(self) -> list[ClientRegistration]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT client_id, client_name, redirect_uris, created_at FROM clients "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [
            ClientRegistration(
                client_id=r[0],
                client_name=r[1],
                redirect_uris=json.loads(r[2]),
                created_at=_from_epoch(int(r[3])),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Authorization codes (single-use, 10-minute TTL)
    # ------------------------------------------------------------------

    async def save_authorization_code(
        self,
        *,
        client_id: str,
        user_id: str,
        redirect_uri: str,
        code_challenge: str,
        ttl_seconds: int = 600,
    ) -> AuthorizationCode:
        now = _utc_now()
        expires_at = _from_epoch(_to_epoch(now) + ttl_seconds)
        record = AuthorizationCode(
            code=_new_token(AUTH_CODE_BYTES),
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            expires_at=expires_at,
            created_at=now,
        )
        await asyncio.to_thread(self._insert_authorization_code_sync, record)
        return record

    def _insert_authorization_code_sync(self, record: AuthorizationCode) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO authorization_codes (
                    code, client_id, user_id, redirect_uri,
                    code_challenge, code_challenge_method,
                    expires_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.code,
                    record.client_id,
                    record.user_id,
                    record.redirect_uri,
                    record.code_challenge,
                    record.code_challenge_method,
                    _to_epoch(record.expires_at),
                    _to_epoch(record.created_at),
                ),
            )
            conn.commit()

    async def consume_authorization_code(self, code: str) -> AuthorizationCode | None:
        """Atomically read + delete an authorization code.

        Returns None if the code does not exist OR is expired (in which
        case the row is still deleted to keep the table small).
        """
        return await asyncio.to_thread(self._consume_authorization_code_sync, code)

    def _consume_authorization_code_sync(self, code: str) -> AuthorizationCode | None:
        with self._connect() as conn:
            # Single transaction: SELECT + DELETE so two clients trying
            # the same code can never both succeed.
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT code, client_id, user_id, redirect_uri, "
                    "code_challenge, code_challenge_method, expires_at, created_at "
                    "FROM authorization_codes WHERE code = ?",
                    (code,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute("DELETE FROM authorization_codes WHERE code = ?", (code,))
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise

        expires_at = _from_epoch(int(row[6]))
        if expires_at < _utc_now():
            return None
        return AuthorizationCode(
            code=row[0],
            client_id=row[1],
            user_id=row[2],
            redirect_uri=row[3],
            code_challenge=row[4],
            code_challenge_method=row[5],
            expires_at=expires_at,
            created_at=_from_epoch(int(row[7])),
        )

    # ------------------------------------------------------------------
    # Access + refresh tokens
    # ------------------------------------------------------------------

    async def save_access_token(
        self,
        *,
        client_id: str,
        user_id: str,
        ttl_seconds: int = 3600,
        with_refresh_token: bool = True,
    ) -> AccessToken:
        now = _utc_now()
        record = AccessToken(
            token=_new_token(ACCESS_TOKEN_BYTES),
            client_id=client_id,
            user_id=user_id,
            expires_at=_from_epoch(_to_epoch(now) + ttl_seconds),
            refresh_token=(_new_token(REFRESH_TOKEN_BYTES) if with_refresh_token else None),
            created_at=now,
            last_used_at=now,
        )
        await asyncio.to_thread(self._insert_access_token_sync, record)
        return record

    def _insert_access_token_sync(self, record: AccessToken) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO access_tokens (
                    token, client_id, user_id, expires_at, refresh_token,
                    created_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token,
                    record.client_id,
                    record.user_id,
                    _to_epoch(record.expires_at),
                    record.refresh_token,
                    _to_epoch(record.created_at),
                    _to_epoch(record.last_used_at),
                ),
            )
            conn.commit()

    async def get_access_token(self, token: str) -> AccessToken | None:
        """Lookup a token and bump ``last_used_at`` if it is still valid."""
        return await asyncio.to_thread(self._get_access_token_sync, token)

    def _get_access_token_sync(self, token: str) -> AccessToken | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token, client_id, user_id, expires_at, refresh_token, "
                "created_at, last_used_at FROM access_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            expires_at = _from_epoch(int(row[3]))
            if expires_at < _utc_now():
                conn.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
                conn.commit()
                return None
            now_epoch = _to_epoch(_utc_now())
            conn.execute(
                "UPDATE access_tokens SET last_used_at = ? WHERE token = ?",
                (now_epoch, token),
            )
            conn.commit()
        return AccessToken(
            token=row[0],
            client_id=row[1],
            user_id=row[2],
            expires_at=expires_at,
            refresh_token=row[4],
            created_at=_from_epoch(int(row[5])),
            last_used_at=_from_epoch(now_epoch),
        )

    async def delete_access_token(self, token: str) -> bool:
        return await asyncio.to_thread(self._delete_access_token_sync, token)

    def _delete_access_token_sync(self, token: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
            conn.commit()
            return cur.rowcount > 0

    async def rotate_refresh_token(
        self,
        *,
        refresh_token: str,
        expected_client_id: str,
        ttl_seconds: int = 3600,
        with_refresh_token: bool = True,
    ) -> AccessToken | None:
        """Atomically swap an old refresh_token row for a new access_token row.

        The SELECT, DELETE, and INSERT happen inside a single
        ``BEGIN IMMEDIATE`` transaction so that a crashed or racing call
        cannot leave the user without an access token. Returns ``None``
        without destructive action when ``refresh_token`` is unknown OR
        the binding ``client_id`` doesn't match — a typo on
        ``client_id`` therefore does NOT cost the user their token.
        """
        return await asyncio.to_thread(
            self._rotate_refresh_token_sync,
            refresh_token,
            expected_client_id,
            ttl_seconds,
            with_refresh_token,
        )

    def _rotate_refresh_token_sync(
        self,
        refresh_token: str,
        expected_client_id: str,
        ttl_seconds: int,
        with_refresh_token: bool,
    ) -> AccessToken | None:
        now = _utc_now()
        new_token_value = _new_token(ACCESS_TOKEN_BYTES)
        new_refresh = _new_token(REFRESH_TOKEN_BYTES) if with_refresh_token else None
        expires_at = _from_epoch(_to_epoch(now) + ttl_seconds)
        client_id: str | None = None
        user_id: str | None = None

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT client_id, user_id FROM access_tokens WHERE refresh_token = ?",
                    (refresh_token,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                if row[0] != expected_client_id:
                    # Mismatched client — refuse without deletion. Leaks no
                    # info about token validity (caller surfaces invalid_grant
                    # either way).
                    conn.execute("COMMIT")
                    return None

                client_id = row[0]
                user_id = row[1]

                conn.execute(
                    "DELETE FROM access_tokens WHERE refresh_token = ?",
                    (refresh_token,),
                )
                conn.execute(
                    """
                    INSERT INTO access_tokens (
                        token, client_id, user_id, expires_at, refresh_token,
                        created_at, last_used_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_token_value,
                        client_id,
                        user_id,
                        _to_epoch(expires_at),
                        new_refresh,
                        _to_epoch(now),
                        _to_epoch(now),
                    ),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise

        # client_id and user_id are guaranteed populated past the early-returns.
        assert client_id is not None and user_id is not None
        return AccessToken(
            token=new_token_value,
            client_id=client_id,
            user_id=user_id,
            expires_at=expires_at,
            refresh_token=new_refresh,
            created_at=now,
            last_used_at=now,
        )

    async def list_tokens_for_user(self, user_id: str) -> list[AccessToken]:
        return await asyncio.to_thread(self._list_tokens_for_user_sync, user_id)

    def _list_tokens_for_user_sync(self, user_id: str) -> list[AccessToken]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token, client_id, user_id, expires_at, refresh_token, "
                "created_at, last_used_at FROM access_tokens WHERE user_id = ? "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [
            AccessToken(
                token=r[0],
                client_id=r[1],
                user_id=r[2],
                expires_at=_from_epoch(int(r[3])),
                refresh_token=r[4],
                created_at=_from_epoch(int(r[5])),
                last_used_at=_from_epoch(int(r[6])),
            )
            for r in rows
        ]

    async def list_access_tokens(self) -> list[AccessToken]:
        """For operator tooling — never expose to MCP clients."""
        return await asyncio.to_thread(self._list_access_tokens_sync)

    def _list_access_tokens_sync(self) -> list[AccessToken]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token, client_id, user_id, expires_at, refresh_token, "
                "created_at, last_used_at FROM access_tokens ORDER BY created_at DESC"
            ).fetchall()
        return [
            AccessToken(
                token=r[0],
                client_id=r[1],
                user_id=r[2],
                expires_at=_from_epoch(int(r[3])),
                refresh_token=r[4],
                created_at=_from_epoch(int(r[5])),
                last_used_at=_from_epoch(int(r[6])),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Service sessions (per-user encrypted Service API session)
    # ------------------------------------------------------------------

    async def upsert_service_session(self, session: ServiceSession) -> None:
        await asyncio.to_thread(self._upsert_service_session_sync, session)

    def _upsert_service_session_sync(self, session: ServiceSession) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO service_sessions (
                    user_id, company_group, encrypted_api_key, encrypted_secret_key,
                    session_id, session_expire, user_type, app_package, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    company_group = excluded.company_group,
                    encrypted_api_key = excluded.encrypted_api_key,
                    encrypted_secret_key = excluded.encrypted_secret_key,
                    session_id = excluded.session_id,
                    session_expire = excluded.session_expire,
                    user_type = excluded.user_type,
                    app_package = excluded.app_package,
                    updated_at = excluded.updated_at
                """,
                (
                    session.user_id,
                    session.company_group,
                    session.encrypted_api_key,
                    session.encrypted_secret_key,
                    session.session_id,
                    session.session_expire,
                    session.user_type,
                    session.app_package,
                    _to_epoch(session.updated_at),
                ),
            )
            conn.commit()

    async def get_service_session(self, user_id: str) -> ServiceSession | None:
        return await asyncio.to_thread(self._get_service_session_sync, user_id)

    def _get_service_session_sync(self, user_id: str) -> ServiceSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, company_group, encrypted_api_key, encrypted_secret_key, "
                "session_id, session_expire, user_type, app_package, updated_at "
                "FROM service_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return ServiceSession(
            user_id=row[0],
            company_group=row[1],
            encrypted_api_key=row[2],
            encrypted_secret_key=row[3],
            session_id=row[4],
            session_expire=int(row[5]),
            user_type=row[6],
            app_package=row[7],
            updated_at=_from_epoch(int(row[8])),
        )

    async def list_service_sessions(self) -> list[ServiceSession]:
        return await asyncio.to_thread(self._list_service_sessions_sync)

    def _list_service_sessions_sync(self) -> list[ServiceSession]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, company_group, encrypted_api_key, encrypted_secret_key, "
                "session_id, session_expire, user_type, app_package, updated_at "
                "FROM service_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [
            ServiceSession(
                user_id=r[0],
                company_group=r[1],
                encrypted_api_key=r[2],
                encrypted_secret_key=r[3],
                session_id=r[4],
                session_expire=int(r[5]),
                user_type=r[6],
                app_package=r[7],
                updated_at=_from_epoch(int(r[8])),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection with the required pragmas.

        Connections are created per call rather than cached because
        sqlite3 connections are bound to the thread that opened them; the
        ``asyncio.to_thread`` callable owns the connection for the
        duration of one operation, then closes it.
        """
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=10.0)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _from_epoch(epoch_seconds: int) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _new_token(num_bytes: int) -> str:
    """Generate an opaque URL-safe token. Uses :mod:`secrets`."""
    return secrets.token_urlsafe(num_bytes)
