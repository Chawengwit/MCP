from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from src.config import ApiAuthConfig, ApiConfig
from src.oauth_provider import Encryptor, OAuthStore, ServiceSessionStore
from src.oauth_provider.schemas import SessionInfo


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def encryptor(fernet_key: str) -> Encryptor:
    return Encryptor(fernet_key.encode("ascii"))


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[OAuthStore]:
    """Fresh OAuthStore backed by a tmp-dir SQLite file."""
    s = OAuthStore(tmp_path / "oauth_provider.db")
    await s.init_db()
    yield s


@pytest.fixture
def session_login_config() -> ApiConfig:
    return ApiConfig(
        type="rest",
        base_url="https://service-api.example.com",
        auth=ApiAuthConfig(
            type="session_login",
            login_path="/api/v1/auth/login",
            login_method="POST",
            credentials={"api_key": "{api_key}", "secret_key": "{secret_key}"},
            session_id_field="data.session_id",
            session_expire_field="data.session_expire",
            user_id_field="data.user_id",
            session_header="X-Session-Id",
            session_format="{session_id}",
        ),
    )


@pytest.fixture
def fake_session() -> SessionInfo:
    return SessionInfo(
        user_id="user-42",
        session_id="svc-session-abc123",
        session_expire=2_000_000_000,
        company_group="acme",
        user_type="employee",
    )


@pytest.fixture
async def service_session_store(
    store: OAuthStore,
    encryptor: Encryptor,
    session_login_config: ApiConfig,
    fake_session: SessionInfo,
) -> ServiceSessionStore:
    async def stub_authenticate(_config, *, api_key, secret_key):  # noqa: ANN001 — stub
        return fake_session

    return ServiceSessionStore(
        store=store,
        encryptor=encryptor,
        api_config=session_login_config,
        authenticate=stub_authenticate,
    )
