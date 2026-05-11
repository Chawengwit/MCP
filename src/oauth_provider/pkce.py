"""PKCE (RFC 7636) — code_challenge / code_verifier verification.

The flow:
  1. Client generates a random ``code_verifier`` (43-128 chars, ``[A-Z]
     [a-z][0-9]-._~``) and the corresponding
     ``code_challenge = BASE64URL-NO-PADDING( SHA256( code_verifier ) )``.
  2. ``code_challenge`` is sent on ``/authorize`` and stored alongside
     the authorization code.
  3. At ``/token`` the client sends ``code_verifier``; this module
     verifies that re-hashing it produces the stored challenge.

Comparison uses :func:`secrets.compare_digest` on bytes, so timing
leaks are not a risk.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

# RFC 7636 §4.1: code_verifier MUST be 43-128 chars from the unreserved set.
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")

# Only S256 is supported per CLAUDE.md security rules — `plain` is
# explicitly out of scope (MCP servers MUST reject it with
# `invalid_request`).
SUPPORTED_METHODS: frozenset[str] = frozenset({"S256"})


def is_valid_verifier(verifier: str) -> bool:
    """True iff `verifier` matches RFC 7636 §4.1 syntax."""
    return bool(_VERIFIER_RE.fullmatch(verifier))


def derive_challenge(verifier: str) -> str:
    """Derive ``BASE64URL-NO-PADDING( SHA256( verifier ) )`` from a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_code_challenge(
    *,
    verifier: str,
    challenge: str,
    method: str = "S256",
) -> bool:
    """Validate that `verifier` matches the stored `challenge`.

    Returns False (not raises) on any of:
      - method not in :data:`SUPPORTED_METHODS`
      - verifier fails the RFC 7636 character/length check
      - challenge bytes mismatch (constant-time compare)

    Callers that need an error code (e.g. ``invalid_request`` vs
    ``invalid_grant``) interpret the False return at the call site.
    """
    if method not in SUPPORTED_METHODS:
        return False
    if not is_valid_verifier(verifier):
        return False
    derived = derive_challenge(verifier).encode("ascii")
    expected = challenge.encode("ascii")
    if len(derived) != len(expected):
        return False
    return secrets.compare_digest(derived, expected)
