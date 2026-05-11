from __future__ import annotations

import asyncio
import time

from src.auth.credentials import AuthRequiredError
from src.config import ApiConfig
from src.oauth_provider import Encryptor, OAuthStore, ServiceSessionStore
from src.oauth_provider.schemas import SessionInfo


async def test_save_then_get_returns_decrypted_session(
    store: OAuthStore,
    encryptor: Encryptor,
    session_login_config: ApiConfig,
    fake_session: SessionInfo,
) -> None:
    async def stub_authenticate(_cfg, *, api_key, secret_key):
        return fake_session

    sstore = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=session_login_config,
        authenticate=stub_authenticate,
    )
    await sstore.save(
        user_id="user-1",
        api_key="A",
        secret_key="B",
        session=fake_session.model_copy(update={"user_id": "user-1"}),
    )
    got = await sstore.get("user-1")
    assert got.user_id == "user-1"
    assert got.session_id == fake_session.session_id


async def test_get_with_no_stored_session_raises(
    store: OAuthStore,
    encryptor: Encryptor,
    session_login_config: ApiConfig,
    fake_session: SessionInfo,
) -> None:
    async def stub_authenticate(_cfg, *, api_key, secret_key):
        return fake_session

    sstore = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=session_login_config,
        authenticate=stub_authenticate,
    )
    try:
        await sstore.get("missing")
    except AuthRequiredError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Expected AuthRequiredError")


async def test_refresh_runs_once_under_concurrency(
    store: OAuthStore,
    encryptor: Encryptor,
    session_login_config: ApiConfig,
) -> None:
    """Many concurrent gets on the same near-expiry user_id => one refresh."""
    expired_session = SessionInfo(
        user_id="user-1",
        session_id="old-sid",
        session_expire=int(time.time()) - 10,  # expired
    )
    fresh_session = SessionInfo(
        user_id="user-1",
        session_id="new-sid",
        session_expire=int(time.time()) + 3600,
    )
    call_count = {"n": 0}

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        call_count["n"] += 1
        # Yield to allow the rest of the gather() coroutines to enter
        # the lock-waiting path.
        await asyncio.sleep(0.01)
        return fresh_session

    sstore = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=session_login_config,
        authenticate=stub_authenticate,
    )
    await sstore.save(user_id="user-1", api_key="A", secret_key="B", session=expired_session)
    # Fire 10 concurrent fetches.
    results = await asyncio.gather(*[sstore.get("user-1") for _ in range(10)])
    assert call_count["n"] == 1, "expected exactly one refresh under contention"
    assert all(r.session_id == "new-sid" for r in results)


async def test_refresh_propagates_auth_required(
    store: OAuthStore,
    encryptor: Encryptor,
    session_login_config: ApiConfig,
) -> None:
    expired_session = SessionInfo(
        user_id="user-1",
        session_id="old",
        session_expire=int(time.time()) - 10,
    )

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        raise AuthRequiredError("rotated")

    sstore = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=session_login_config,
        authenticate=stub_authenticate,
    )
    await sstore.save(user_id="user-1", api_key="A", secret_key="B", session=expired_session)
    try:
        await sstore.get("user-1")
    except AuthRequiredError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Expected AuthRequiredError to propagate")


async def test_lock_per_user_id_separates_users(
    store: OAuthStore,
    encryptor: Encryptor,
    session_login_config: ApiConfig,
) -> None:
    fresh = SessionInfo(user_id="u1", session_id="s", session_expire=int(time.time()) + 3600)

    async def stub_authenticate(_cfg, *, api_key, secret_key):
        return fresh

    sstore = ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=session_login_config,
        authenticate=stub_authenticate,
    )
    lock1 = sstore._lock_for("user-A")
    lock2 = sstore._lock_for("user-B")
    assert lock1 is not lock2
    # Same user_id => same lock object
    assert sstore._lock_for("user-A") is lock1
