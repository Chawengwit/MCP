"""Service API ``session_login`` client.

POSTs ``api_key`` + ``secret_key`` to the configured Service API
``login_path`` and parses the response into a
:class:`src.oauth_provider.schemas.SessionInfo`.

The Service API itself is **not** modified in Phase 9 — it remains a
PHP-style endpoint that returns a session token. This module is the
minimum-viable adapter we need to drive that endpoint from an asyncio
context.

Failure modes:
  - HTTP 4xx → ``AuthRequiredError`` (caller maps to invalid_grant /
    re-render of the consent form).
  - HTTP 5xx, network errors, malformed JSON, missing fields →
    ``AuthError`` (caller maps to 502 / generic error banner).
  - Logging is intentionally event-only — no api_key, secret_key, or
    session_id is ever interpolated into a log line.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.auth.credentials import AuthError, AuthRequiredError
from src.config import ApiAuthConfig, ApiConfig
from src.oauth_provider.schemas import SessionInfo

logger = logging.getLogger("mcp.auth.service_api")


class ServiceApiConfigurationError(AuthError):
    """Raised when an ``ApiAuthConfig`` is missing the session_login fields."""


async def authenticate(
    config: ApiConfig,
    *,
    api_key: str,
    secret_key: str,
) -> SessionInfo:
    """Exchange (api_key, secret_key) for a Service API session.

    The HTTP request is constructed entirely from the ``ApiConfig``;
    the only call-site secret is the credential pair. Successful
    responses are parsed via dotted-path lookup into a
    :class:`SessionInfo`. Missing fields are mapped to
    ``AuthRequiredError`` (not 500) because the most likely cause is
    "the operator has the wrong response_field paths configured".
    """
    auth_config = config.auth
    if auth_config is None or (auth_config.type or "").lower() != "session_login":
        raise ServiceApiConfigurationError("Service API config must have auth.type=session_login.")

    login_url, method, payload, headers = _build_request(
        config=config,
        auth=auth_config,
        api_key=api_key,
        secret_key=secret_key,
    )

    timeout = config.limits.timeout_seconds if config.limits else 30
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, login_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # Network-layer failure (DNS, timeout, connection reset). Keep the
        # exception class name in the log but not the URL — the URL can
        # contain tenant identifiers operators consider sensitive.
        logger.warning("Service API login transport failure: %s", type(exc).__name__)
        raise AuthError(f"Service API unreachable: {type(exc).__name__}") from exc

    if response.status_code >= 500:
        logger.warning("Service API login returned status %s", response.status_code)
        raise AuthError(f"Service API returned {response.status_code}")

    if response.status_code >= 400:
        # Treat all 4xx as authentication failure. We do NOT echo the
        # Service API body to the caller — it may contain identifiers
        # that should not appear in browser-rendered error banners.
        logger.info("Service API login rejected (status=%s)", response.status_code)
        raise AuthRequiredError("Service API rejected the supplied credentials.")

    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("Service API returned a non-JSON body.") from exc

    if not isinstance(body, dict):
        raise AuthError("Service API returned a non-object JSON body.")

    return _parse_session(auth_config, body)


# ----------------------------------------------------------------------
# Building the request
# ----------------------------------------------------------------------


def _build_request(
    *,
    config: ApiConfig,
    auth: ApiAuthConfig,
    api_key: str,
    secret_key: str,
) -> tuple[str, str, dict[str, str], dict[str, str]]:
    login_path = (auth.login_path or "").lstrip("/")
    if not login_path:
        raise ServiceApiConfigurationError("auth.login_path is required for session_login.")

    method = (auth.login_method or "POST").upper()
    base = config.base_url.rstrip("/")
    login_url = f"{base}/{login_path}"

    credentials_template = auth.credentials or {}
    if not isinstance(credentials_template, dict) or not credentials_template:
        # Fall back to a sensible default for the most common Service API shape.
        credentials_template = {"api_key": "{api_key}", "secret_key": "{secret_key}"}

    payload: dict[str, str] = {}
    for field, template in credentials_template.items():
        if not isinstance(template, str):
            raise ServiceApiConfigurationError(
                "auth.credentials values must be strings (with {api_key}/{secret_key} placeholders)."
            )
        rendered = template.format(api_key=api_key, secret_key=secret_key)
        payload[field] = rendered

    headers: dict[str, str] = {
        "accept": "application/json",
        "content-type": "application/json",
    }
    return login_url, method, payload, headers


# ----------------------------------------------------------------------
# Parsing the response
# ----------------------------------------------------------------------


def _parse_session(auth: ApiAuthConfig, body: dict[str, Any]) -> SessionInfo:
    session_id = _dotted_lookup(body, auth.session_id_field or "session_id")
    session_expire = _dotted_lookup(body, auth.session_expire_field or "session_expire")
    user_id = _dotted_lookup(body, auth.user_id_field or "user_id")

    missing: list[str] = []
    if session_id is None:
        missing.append(auth.session_id_field or "session_id")
    if session_expire is None:
        missing.append(auth.session_expire_field or "session_expire")
    if user_id is None:
        missing.append(auth.user_id_field or "user_id")
    if missing:
        raise AuthRequiredError(
            "Service API response is missing required field(s); the operator "
            "may need to set session_id_field / session_expire_field / user_id_field "
            "to match the upstream response shape."
        )

    try:
        session_expire_int = int(session_expire)
    except (TypeError, ValueError) as exc:
        raise AuthError("Service API returned a non-integer session_expire.") from exc

    return SessionInfo(
        user_id=str(user_id),
        session_id=str(session_id),
        session_expire=session_expire_int,
        company_group=_optional_str(_dotted_lookup(body, "company_group")),
        user_type=_optional_str(_dotted_lookup(body, "user_type")),
        app_package=_optional_str(_dotted_lookup(body, "app_package")),
    )


def _dotted_lookup(body: dict[str, Any], dotted_path: str) -> Any:
    """Read ``body[a][b][c]`` for ``dotted_path = "a.b.c"``.

    Returns None for any missing intermediate key (Service APIs vary; a
    None here is treated as "field absent" by the caller).
    """
    current: Any = body
    for key in dotted_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
