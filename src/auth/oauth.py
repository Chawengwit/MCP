from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.events.redaction import redact_url

logger = logging.getLogger("mcp.auth.oauth")

DEFAULT_CALLBACK_PORT = 8765
DEFAULT_CALLBACK_TIMEOUT_SEC = 300.0
TOKEN_EXCHANGE_TIMEOUT_SEC = 30.0


class OAuthError(RuntimeError):
    """OAuth flow failure (port conflict, state mismatch, token exchange error, etc.)."""


class TokenInfo(BaseModel):
    """Tokens returned by the OAuth provider, plus computed expiry.

    Secret fields use `repr=False` so `repr(token)`, f-string interpolation, and
    default logging formatters cannot leak token values. `model_dump_json()` still
    includes them (needed for keyring serialization) — call only at the storage
    boundary, never in log lines.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str = Field(repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    expires_at: float  # absolute Unix timestamp
    token_type: str = "Bearer"
    scope: str | None = None


class OAuthConfig(BaseModel):
    """Configuration for one OAuth 2.0 provider.

    `client_secret` uses `repr=False` to keep the value out of repr/log output.
    `authorize_url` and `token_url` are validated to require HTTPS — sending the
    auth code or client_secret over plaintext would expose them on the wire.
    """

    model_config = ConfigDict(extra="ignore")

    provider: str
    client_id: str
    client_secret: str = Field(repr=False)
    authorize_url: str
    token_url: str
    scopes: list[str]
    redirect_uri: str | None = None  # default computed from OAUTH_CALLBACK_PORT

    @field_validator("authorize_url", "token_url")
    @classmethod
    def _https_required(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(
                f"OAuth URLs must use https:// (got {v!r}). "
                f"Plaintext OAuth flows would expose client_secret and auth codes."
            )
        return v


class OAuth:
    """OAuth 2.0 authorization-code flow with PKCE.

    Phase 3 only handles `auth.type == "oauth2"`. Bearer-token APIs and
    unauthenticated APIs are handled by Phase 5 tools directly.
    """

    def __init__(self, *, callback_port: int | None = None) -> None:
        if callback_port is not None:
            self._callback_port = callback_port
        else:
            self._callback_port = int(os.getenv("OAUTH_CALLBACK_PORT", DEFAULT_CALLBACK_PORT))

    @property
    def callback_port(self) -> int:
        return self._callback_port

    @property
    def default_redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._callback_port}/callback"

    async def start_flow(self, config: OAuthConfig) -> TokenInfo:
        """Drive the full OAuth flow: authorize → callback → token exchange."""
        verifier, challenge, state = _generate_pkce()
        redirect_uri = config.redirect_uri or self.default_redirect_uri

        authorize_url = _build_authorize_url(
            config=config,
            challenge=challenge,
            state=state,
            redirect_uri=redirect_uri,
        )
        logger.info("OAuth flow started for provider %s", config.provider)
        # Do NOT log the full authorize_url at INFO — it leaks client_id and PKCE
        # challenge to log aggregators. DEBUG-level redaction strips query secrets.
        logger.debug("authorize_url=%s", redact_url(authorize_url))

        webbrowser.open(authorize_url, new=2)

        code = await self._run_callback_server(self._callback_port, expected_state=state)
        return await self._exchange_code(
            config=config,
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
        )

    async def refresh(self, config: OAuthConfig, refresh_token: str) -> TokenInfo:
        """Exchange a refresh_token for a new access_token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }
        return await self._post_token_endpoint(config.token_url, data)

    # ------------------------------------------------------------------
    # Private helpers (instance methods so tests can monkeypatch them)
    # ------------------------------------------------------------------

    async def _run_callback_server(self, port: int, expected_state: str) -> str:
        """Bind a one-shot HTTP server on 127.0.0.1, return the auth code.

        SECURITY: host MUST be `127.0.0.1` exactly. Never `0.0.0.0` or `localhost` —
        browsers may treat localhost vs 127.0.0.1 as different origins for OAuth state.
        """
        loop = asyncio.get_running_loop()
        code_future: asyncio.Future[str] = loop.create_future()

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = (await reader.readline()).decode("ascii", errors="replace")
                if not request_line:
                    return
                parts = request_line.split(" ", 2)
                if len(parts) < 2 or parts[0] != "GET":
                    await _write_http(writer, 405, "Method Not Allowed")
                    return

                # Drain remaining headers.
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break

                parsed = urllib.parse.urlparse(parts[1])
                params = dict(urllib.parse.parse_qsl(parsed.query))

                if params.get("state") != expected_state:
                    await _write_http(writer, 400, "State parameter mismatch.")
                    if not code_future.done():
                        code_future.set_exception(OAuthError("OAuth state mismatch"))
                    return

                code = params.get("code")
                if not code:
                    await _write_http(writer, 400, "Missing authorization code.")
                    if not code_future.done():
                        code_future.set_exception(OAuthError("Missing authorization code"))
                    return

                await _write_http(
                    writer,
                    200,
                    "Authentication complete. You can close this tab.",
                )
                if not code_future.done():
                    code_future.set_result(code)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

        try:
            server = await asyncio.start_server(handle_client, host="127.0.0.1", port=port)
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise OAuthError(
                    f"OAuth callback port {port} is in use or not permitted. "
                    f"Set OAUTH_CALLBACK_PORT to a free port and retry."
                ) from exc
            raise

        try:
            async with server:
                return await asyncio.wait_for(
                    code_future,
                    timeout=DEFAULT_CALLBACK_TIMEOUT_SEC,
                )
        finally:
            # `async with server` already closed the server; this is belt-and-braces.
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

    async def _exchange_code(
        self,
        *,
        config: OAuthConfig,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> TokenInfo:
        """Exchange the authorization code for tokens (PKCE)."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code_verifier": verifier,
        }
        return await self._post_token_endpoint(config.token_url, data)

    async def _post_token_endpoint(self, token_url: str, data: dict[str, str]) -> TokenInfo:
        """POST to the token endpoint and parse the response into TokenInfo.

        Body fields contain secrets (client_secret, refresh_token) and are NEVER logged.
        """
        async with httpx.AsyncClient(timeout=TOKEN_EXCHANGE_TIMEOUT_SEC) as client:
            response = await client.post(
                token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
        if response.status_code >= 400:
            # Body may include 'error_description' but never the request body.
            raise OAuthError(
                f"Token endpoint returned HTTP {response.status_code} from {redact_url(token_url)}"
            )
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise OAuthError(
                f"Token endpoint returned non-JSON response from {redact_url(token_url)}"
            ) from exc

        access_token = body.get("access_token")
        if not access_token:
            raise OAuthError("Token endpoint response missing 'access_token'")
        # Some providers (e.g. older Microsoft endpoints) return expires_in as a
        # string. Try to coerce; fall back to 1 hour only if absent or unparseable.
        expires_in_seconds = _parse_expires_in(body.get("expires_in"))
        return TokenInfo(
            access_token=access_token,
            refresh_token=body.get("refresh_token"),
            expires_at=time.time() + expires_in_seconds,
            token_type=body.get("token_type", "Bearer"),
            scope=body.get("scope"),
        )


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions — easier to unit-test in isolation)
# ---------------------------------------------------------------------------


DEFAULT_EXPIRES_IN_SEC = 3600.0


def _parse_expires_in(value: Any) -> float:
    """Coerce a token endpoint's `expires_in` field to seconds (float).

    Accepts int, float, or numeric string. Returns 3600 (1 hour) if absent
    or unparseable, matching common provider behavior.
    """
    if value is None:
        return DEFAULT_EXPIRES_IN_SEC
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return DEFAULT_EXPIRES_IN_SEC
    return DEFAULT_EXPIRES_IN_SEC


def _generate_pkce() -> tuple[str, str, str]:
    """Return (code_verifier, code_challenge, state) per RFC 7636.

    code_verifier: 43–128 char URL-safe string. secrets.token_urlsafe(64) yields ~86
    chars from [A-Za-z0-9-_], all of which are in the RFC-allowed alphabet.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    return verifier, challenge, state


def _build_authorize_url(
    *,
    config: OAuthConfig,
    challenge: str,
    state: str,
    redirect_uri: str,
) -> str:
    """Construct the provider's authorize URL with all PKCE parameters."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in config.authorize_url else "?"
    return f"{config.authorize_url}{sep}{urllib.parse.urlencode(params)}"


async def _write_http(writer: asyncio.StreamWriter, status: int, body: str) -> None:
    reason = {200: "OK", 400: "Bad Request", 405: "Method Not Allowed"}.get(status, "Error")
    payload = body.encode("utf-8")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n".encode("ascii")
    )
    writer.write(payload)
    with contextlib.suppress(Exception):
        await writer.drain()
