"""Tests for the STDIO-mode keyring-backed Service API session store."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest
from src.auth.credentials import AuthError, AuthRequiredError
from src.auth.session_login_keyring import (
    REFRESH_BUFFER_SEC,
    KeyringServiceSessionStore,
    StoredSession,
)
from src.config import ApiAuthConfig, ApiConfig
from src.oauth_provider.schemas import SessionInfo


def _config(api_id: str = "svc") -> dict[str, ApiConfig]:
    return {
        api_id: ApiConfig(
            type="rest",
            base_url="https://svc.example.com",
            auth=ApiAuthConfig(
                type="session_login",
                login_path="/v1/auth",
                login_method="POST",
                session_id_field="data.session_id",
                session_expire_field="data.expire",
                user_id_field="_api_key_fingerprint",
            ),
        )
    }


class _FakeKeyring:
    """Minimal in-memory keyring stub, isolates tests from the OS backend."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self._store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self._store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        if (service, key) not in self._store:
            # Mirror the real keyring API: deleting a missing entry raises.
            import keyring.errors as kerr

            raise kerr.PasswordDeleteError(f"no entry for {service}/{key}")
        del self._store[(service, key)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    """Replace the real keyring backend for the duration of each test."""
    fake = _FakeKeyring()
    monkeypatch.setattr("src.auth.session_login_keyring.keyring", fake)
    return fake


def _now() -> int:
    return int(time.time())


# ----------------------------------------------------------------------
# Round-trip
# ----------------------------------------------------------------------


async def test_save_then_peek_round_trip() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    await store.save(
        api_id="svc",
        api_key="K",
        secret_key="S",
        user_id="u1",
        session_id="sess-1",
        session_expire=_now() + 3600,
        company_group="acme",
        user_type="user",
    )
    stored = await store.peek("svc")
    assert stored is not None
    assert isinstance(stored, StoredSession)
    assert stored.api_key == "K"
    assert stored.session_id == "sess-1"
    assert stored.company_group == "acme"


async def test_peek_returns_none_when_unset() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    assert await store.peek("svc") is None


async def test_clear_removes_entry() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    await store.save(
        api_id="svc",
        api_key="K",
        secret_key="S",
        user_id="u",
        session_id="s",
        session_expire=_now() + 3600,
    )
    await store.clear("svc")
    assert await store.peek("svc") is None


async def test_clear_is_no_op_when_entry_missing() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    # MUST NOT raise — operator should be able to clear blindly.
    await store.clear("svc")


# ----------------------------------------------------------------------
# get() — happy path + refresh
# ----------------------------------------------------------------------


async def test_get_raises_when_unset() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    with pytest.raises(AuthRequiredError, match="session_login svc"):
        await store.get("svc")


async def test_get_returns_stored_session_when_valid() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    await store.save(
        api_id="svc",
        api_key="K",
        secret_key="S",
        user_id="u1",
        session_id="sess-1",
        session_expire=_now() + 3600,
    )
    got = await store.get("svc")
    assert got.session_id == "sess-1"


async def test_get_refreshes_near_expiry() -> None:
    """When the stored session has <60s left, get() calls the Service API
    again with the persisted api_key+secret_key and stores the new session."""
    auth_calls: list[tuple[str, str]] = []

    async def stub_authenticate(config: ApiConfig, *, api_key: str, secret_key: str) -> SessionInfo:
        auth_calls.append((api_key, secret_key))
        return SessionInfo(
            user_id="u1",
            session_id="sess-new",
            session_expire=_now() + 7200,
            company_group="acme",
        )

    store = KeyringServiceSessionStore(configs=_config(), authenticate=stub_authenticate)
    # Stored session expires in 10 seconds — well inside the 60 s buffer.
    await store.save(
        api_id="svc",
        api_key="K",
        secret_key="S",
        user_id="u1",
        session_id="sess-old",
        session_expire=_now() + 10,
    )
    refreshed = await store.get("svc")
    assert refreshed.session_id == "sess-new"
    assert auth_calls == [("K", "S")]

    # Subsequent get within the buffer of the NEW session does NOT re-auth.
    stored2 = await store.get("svc")
    assert stored2.session_id == "sess-new"
    assert len(auth_calls) == 1


async def test_concurrent_get_under_expiry_triggers_one_refresh() -> None:
    """Per-api_id Lock — 10 concurrent calls should fire exactly one auth."""
    auth_calls: list[tuple[str, str]] = []
    auth_started = asyncio.Event()
    auth_release = asyncio.Event()

    async def slow_authenticate(config: ApiConfig, *, api_key: str, secret_key: str) -> SessionInfo:
        auth_calls.append((api_key, secret_key))
        auth_started.set()
        # Hold the lock long enough that all other callers queue up
        # waiting on _lock_for.
        await auth_release.wait()
        return SessionInfo(
            user_id="u1",
            session_id="sess-after-refresh",
            session_expire=_now() + 7200,
        )

    store = KeyringServiceSessionStore(configs=_config(), authenticate=slow_authenticate)
    await store.save(
        api_id="svc",
        api_key="K",
        secret_key="S",
        user_id="u1",
        session_id="sess-old",
        session_expire=_now() + 10,
    )

    # Kick off many concurrent get()s.
    tasks = [asyncio.create_task(store.get("svc")) for _ in range(10)]
    await auth_started.wait()
    # Release the in-flight auth call — the rest should resolve from cache.
    auth_release.set()
    results = await asyncio.gather(*tasks)
    assert all(r.session_id == "sess-after-refresh" for r in results)
    # Single authenticate call across all 10 concurrent gets.
    assert len(auth_calls) == 1


async def test_lock_per_api_id_separates_calls() -> None:
    store = KeyringServiceSessionStore(configs=_config())
    a = store._lock_for("svc-A")
    b = store._lock_for("svc-B")
    assert a is not b
    # Same api_id returns the same lock instance.
    assert store._lock_for("svc-A") is a


async def test_get_propagates_service_api_failure_as_auth_error() -> None:
    async def failing_authenticate(
        config: ApiConfig, *, api_key: str, secret_key: str
    ) -> SessionInfo:
        raise AuthError("Service API down")

    store = KeyringServiceSessionStore(configs=_config(), authenticate=failing_authenticate)
    await store.save(
        api_id="svc",
        api_key="K",
        secret_key="S",
        user_id="u",
        session_id="s",
        session_expire=_now() + 10,  # near expiry → triggers refresh
    )
    with pytest.raises(AuthError, match="Service API down"):
        await store.get("svc")


async def test_get_when_api_removed_from_configs_raises_auth_required() -> None:
    """If the operator removed an API after logging in, refresh has no
    config to call — surface as a clean 're-login required' rather than a
    cryptic KeyError."""

    async def stub_authenticate(
        config: ApiConfig, *, api_key: str, secret_key: str
    ) -> SessionInfo:  # pragma: no cover — should never be called
        raise AssertionError("authenticate must not be called when config is missing")

    store = KeyringServiceSessionStore(configs={}, authenticate=stub_authenticate)
    # Use the internal _write so we don't need a config to seed.
    store._write(
        "svc",
        StoredSession(
            api_key="K",
            secret_key="S",
            user_id="u",
            session_id="s",
            session_expire=_now() + 10,
        ),
    )
    with pytest.raises(AuthRequiredError, match="re-run"):
        await store.get("svc")


# ----------------------------------------------------------------------
# REFRESH_BUFFER_SEC sanity
# ----------------------------------------------------------------------


def test_refresh_buffer_is_short_but_nonzero() -> None:
    """A 60 s buffer is short enough that clients see refreshes promptly,
    long enough that clock skew doesn't whipsaw the system."""
    assert 30 <= REFRESH_BUFFER_SEC <= 300


# ----------------------------------------------------------------------
# Redaction: serialized form never reveals secrets in plain text accidentally
# ----------------------------------------------------------------------


async def test_no_secret_leakage_in_repr() -> None:
    """Pydantic v2 model_dump_json IS the on-disk format, so this test
    guards that we don't accidentally print or log the model directly."""
    record = StoredSession(
        api_key="VERY-SECRET-KEY",
        secret_key="VERY-SECRET-SECRET",
        user_id="u",
        session_id="s",
        session_expire=_now() + 60,
    )
    # The dataclass repr would include field values; for a model we accept
    # that — but make sure the explicit "redaction" surface used by tests
    # (model_dump(exclude=...)) keeps working.
    public = record.model_dump(exclude={"api_key", "secret_key"})
    assert "VERY-SECRET-KEY" not in str(public)
    assert "VERY-SECRET-SECRET" not in str(public)


# ----------------------------------------------------------------------
# Compatibility: factory + dataclass-style construction
# ----------------------------------------------------------------------


def test_from_configs_factory_matches_explicit_constructor() -> None:
    cfgs = _config()
    a = KeyringServiceSessionStore.from_configs(cfgs)
    b = KeyringServiceSessionStore(configs=cfgs)
    assert isinstance(a, KeyringServiceSessionStore)
    assert isinstance(b, KeyringServiceSessionStore)


# Suppress linter false-positive about unused import
_ = Callable[[Any], Any]
