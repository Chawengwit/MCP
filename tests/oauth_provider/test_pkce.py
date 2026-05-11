from __future__ import annotations

import base64
import hashlib

from src.oauth_provider.pkce import (
    derive_challenge,
    is_valid_verifier,
    verify_code_challenge,
)


def test_derive_challenge_matches_rfc7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert derive_challenge(verifier) == expected


def test_verify_code_challenge_happy_path() -> None:
    verifier = "x" * 43
    challenge = derive_challenge(verifier)
    assert verify_code_challenge(verifier=verifier, challenge=challenge) is True


def test_verify_code_challenge_rejects_mismatch() -> None:
    verifier = "x" * 43
    assert verify_code_challenge(verifier=verifier, challenge="bad") is False


def test_verify_code_challenge_rejects_plain_method() -> None:
    verifier = "x" * 43
    challenge = derive_challenge(verifier)
    assert verify_code_challenge(verifier=verifier, challenge=challenge, method="plain") is False


def test_is_valid_verifier_length_boundaries() -> None:
    assert is_valid_verifier("a" * 43) is True
    assert is_valid_verifier("a" * 128) is True
    assert is_valid_verifier("a" * 42) is False
    assert is_valid_verifier("a" * 129) is False


def test_is_valid_verifier_charset() -> None:
    # Unreserved chars only
    assert is_valid_verifier("ABC-._~" + "x" * 36) is True
    # Forbidden chars
    assert is_valid_verifier("x" * 42 + " ") is False
    assert is_valid_verifier("x" * 42 + "/") is False


def test_verify_code_challenge_rejects_invalid_verifier_chars() -> None:
    bad_verifier = "x" * 42 + "/"
    challenge = derive_challenge(bad_verifier)  # not legal under RFC but compute anyway
    assert verify_code_challenge(verifier=bad_verifier, challenge=challenge) is False
