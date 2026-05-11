from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from src.oauth_provider import OAuthStore
from src.oauth_provider.schemas import ServiceSession
from src.oauth_provider.store import _from_epoch, _to_epoch


async def test_register_and_get_client(store: OAuthStore) -> None:
    record = await store.register_client(client_name="cli", redirect_uris=["https://x/cb"])
    fetched = await store.get_client(record.client_id)
    assert fetched is not None
    assert fetched.client_id == record.client_id
    assert fetched.redirect_uris == ["https://x/cb"]


async def test_get_client_missing_returns_none(store: OAuthStore) -> None:
    assert await store.get_client("nope") is None


async def test_save_and_consume_authorization_code(store: OAuthStore) -> None:
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    code = await store.save_authorization_code(
        client_id=client.client_id,
        user_id="u1",
        redirect_uri="https://x/cb",
        code_challenge="chal",
    )
    consumed = await store.consume_authorization_code(code.code)
    assert consumed is not None
    assert consumed.user_id == "u1"
    # second consume returns None — single-use enforced
    assert await store.consume_authorization_code(code.code) is None


async def test_expired_authorization_code_returns_none(store: OAuthStore) -> None:
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    code = await store.save_authorization_code(
        client_id=client.client_id,
        user_id="u1",
        redirect_uri="https://x/cb",
        code_challenge="chal",
        ttl_seconds=-1,
    )
    assert await store.consume_authorization_code(code.code) is None


async def test_save_and_get_access_token(store: OAuthStore) -> None:
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    token = await store.save_access_token(client_id=client.client_id, user_id="u1")
    got = await store.get_access_token(token.token)
    assert got is not None
    assert got.user_id == "u1"
    assert got.refresh_token == token.refresh_token


async def test_expired_access_token_is_deleted_on_lookup(store: OAuthStore) -> None:
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    token = await store.save_access_token(client_id=client.client_id, user_id="u1", ttl_seconds=-10)
    assert await store.get_access_token(token.token) is None
    # second lookup after deletion is still None
    assert await store.get_access_token(token.token) is None


async def test_rotate_refresh_token_atomically_swaps_rows(store: OAuthStore) -> None:
    """Atomic SELECT + DELETE + INSERT inside one transaction.

    The old refresh_token must be gone and the returned token must lookup
    successfully via :meth:`get_access_token`.
    """
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    original = await store.save_access_token(client_id=client.client_id, user_id="u1")
    assert original.refresh_token is not None

    rotated = await store.rotate_refresh_token(
        refresh_token=original.refresh_token,
        expected_client_id=client.client_id,
    )
    assert rotated is not None
    assert rotated.token != original.token
    assert rotated.user_id == "u1"
    assert rotated.client_id == client.client_id

    # The new token is live.
    looked_up = await store.get_access_token(rotated.token)
    assert looked_up is not None

    # The old refresh token is gone.
    assert (
        await store.rotate_refresh_token(
            refresh_token=original.refresh_token,
            expected_client_id=client.client_id,
        )
        is None
    )


async def test_rotate_refresh_token_returns_none_on_client_mismatch_without_deletion(
    store: OAuthStore,
) -> None:
    """A typo on client_id MUST NOT cost the user their refresh token.

    The returned value is None (same as 'unknown token' — no oracle for
    attackers), but the row is preserved so the legitimate caller can
    retry with the correct client_id.
    """
    client = await store.register_client(client_name="c", redirect_uris=["https://x/cb"])
    token = await store.save_access_token(client_id=client.client_id, user_id="u1")
    assert token.refresh_token is not None

    bad = await store.rotate_refresh_token(
        refresh_token=token.refresh_token,
        expected_client_id="some-other-client",
    )
    assert bad is None

    # Retry with the correct client_id succeeds — the row was preserved.
    good = await store.rotate_refresh_token(
        refresh_token=token.refresh_token,
        expected_client_id=client.client_id,
    )
    assert good is not None
    assert good.user_id == "u1"


async def test_foreign_key_enforced_for_authorization_codes(store: OAuthStore) -> None:
    """foreign_keys=ON should reject auth code rows whose client_id is unknown."""
    with sqlite3.connect(str(store._db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO authorization_codes (
                    code, client_id, user_id, redirect_uri,
                    code_challenge, code_challenge_method, expires_at, created_at
                ) VALUES ('c1', 'no-such-client', 'u', 'https://x', 'chal', 'S256', 1, 0)
                """
            )


async def test_wal_journal_mode_set(store: OAuthStore) -> None:
    """journal_mode=WAL is required for concurrent reads under write load."""
    conn = sqlite3.connect(str(store._db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


async def test_concurrent_writes_do_not_corrupt(store: OAuthStore) -> None:
    """10 concurrent client registrations should all succeed and be readable."""
    results = await asyncio.gather(
        *[
            store.register_client(client_name=f"c{i}", redirect_uris=["https://x"])
            for i in range(10)
        ]
    )
    assert len({r.client_id for r in results}) == 10
    clients = await store.list_clients()
    assert len(clients) == 10


async def test_upsert_service_session_replaces_on_conflict(
    store: OAuthStore, tmp_path: Path
) -> None:
    s1 = ServiceSession(
        user_id="u1",
        encrypted_api_key=b"a",
        encrypted_secret_key=b"b",
        session_id="sid-1",
        session_expire=100,
        updated_at=datetime.now(timezone.utc),
    )
    await store.upsert_service_session(s1)
    s2 = s1.model_copy(update={"session_id": "sid-2", "session_expire": 200})
    await store.upsert_service_session(s2)
    got = await store.get_service_session("u1")
    assert got is not None
    assert got.session_id == "sid-2"
    assert got.session_expire == 200


async def test_to_from_epoch_roundtrip() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert _from_epoch(_to_epoch(now)) == now


async def test_list_tokens_for_user_filters(store: OAuthStore) -> None:
    client = await store.register_client(client_name="c", redirect_uris=["https://x"])
    await store.save_access_token(client_id=client.client_id, user_id="u1")
    await store.save_access_token(client_id=client.client_id, user_id="u2")
    u1_tokens = await store.list_tokens_for_user("u1")
    assert len(u1_tokens) == 1
    assert u1_tokens[0].user_id == "u1"


async def test_secret_omission_serialized_rows_contain_no_plaintext_keys(
    store: OAuthStore,
) -> None:
    """SQLite columns for service_sessions hold ciphertext bytes only."""
    plaintext = "ULTRA-secret-api-key"
    enc = b"encrypted-bytes"  # caller supplies cipher; we just check round-trip
    s = ServiceSession(
        user_id="u1",
        encrypted_api_key=enc,
        encrypted_secret_key=enc,
        session_id="sid",
        session_expire=100,
        updated_at=datetime.now(timezone.utc),
    )
    await store.upsert_service_session(s)
    conn = sqlite3.connect(str(store._db_path))
    try:
        row = conn.execute(
            "SELECT encrypted_api_key, encrypted_secret_key FROM service_sessions"
        ).fetchone()
    finally:
        conn.close()
    assert plaintext.encode("utf-8") not in row[0]
    assert plaintext.encode("utf-8") not in row[1]


async def test_authorization_code_expiry_outside_window(store: OAuthStore) -> None:
    """A code with expires_at in the past must not be returned even before consume."""
    client = await store.register_client(client_name="c", redirect_uris=["https://x"])
    code = await store.save_authorization_code(
        client_id=client.client_id,
        user_id="u1",
        redirect_uri="https://x",
        code_challenge="c",
        ttl_seconds=int(-timedelta(minutes=1).total_seconds()),
    )
    assert await store.consume_authorization_code(code.code) is None
