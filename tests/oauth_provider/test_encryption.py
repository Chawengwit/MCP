from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from src.oauth_provider.encryption import (
    ConfigurationError,
    DecryptionError,
    Encryptor,
    is_oauth_provider_enabled,
)


def test_encryptor_roundtrip(fernet_key: str) -> None:
    enc = Encryptor(fernet_key.encode("ascii"))
    cipher = enc.encrypt("hello")
    assert cipher != b"hello"
    assert enc.decrypt(cipher) == "hello"


def test_encryptor_rejects_malformed_key() -> None:
    with pytest.raises(ConfigurationError):
        Encryptor(b"not-a-fernet-key")


def test_encryptor_raises_decryption_error_on_wrong_key() -> None:
    enc1 = Encryptor(Fernet.generate_key())
    cipher = enc1.encrypt("payload")
    enc2 = Encryptor(Fernet.generate_key())
    with pytest.raises(DecryptionError):
        enc2.decrypt(cipher)


def test_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_OAUTH_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        Encryptor.from_env()


def test_from_env_loads_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("MCP_OAUTH_ENCRYPTION_KEY", key)
    enc = Encryptor.from_env()
    assert enc.decrypt(enc.encrypt("ok")) == "ok"


def test_is_oauth_provider_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_OAUTH_ENCRYPTION_KEY", raising=False)
    assert is_oauth_provider_enabled() is False
    monkeypatch.setenv("MCP_OAUTH_ENCRYPTION_KEY", "anything")
    assert is_oauth_provider_enabled() is True


def test_encryption_error_message_does_not_leak_secrets(fernet_key: str) -> None:
    """DecryptionError text MUST NOT include the plaintext/ciphertext or key."""
    enc1 = Encryptor(fernet_key.encode("ascii"))
    cipher = enc1.encrypt("super-secret-payload")
    enc2 = Encryptor(Fernet.generate_key())
    try:
        enc2.decrypt(cipher)
    except DecryptionError as exc:
        assert "super-secret-payload" not in str(exc)
        assert fernet_key not in str(exc)
