from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

REDACTED = "<redacted>"

DEFAULT_HEADER_KEYS: frozenset[str] = frozenset(
    {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
)

DEFAULT_BODY_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "api_key",
        "apikey",
        "secret",
    }
)

DEFAULT_QUERY_KEYS: frozenset[str] = frozenset(
    {"api_key", "apikey", "token", "access_token", "secret", "password"}
)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in DEFAULT_HEADER_KEYS:
            result[key] = REDACTED
        else:
            result[key] = value
    return result


def redact_body(body: Any, extra_keys: frozenset[str] | set[str] | None = None) -> Any:
    keys = DEFAULT_BODY_KEYS
    if extra_keys:
        keys = DEFAULT_BODY_KEYS | frozenset(k.lower() for k in extra_keys)
    return _redact_recursive(body, keys)


def _redact_recursive(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if k.lower() in keys else _redact_recursive(v, keys)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_recursive(item, keys) for item in value]
    return value


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    new_query = _redact_qs(parsed.query) if parsed.query else parsed.query
    new_fragment = _redact_qs(parsed.fragment) if parsed.fragment else parsed.fragment
    if new_query == parsed.query and new_fragment == parsed.fragment:
        return url
    return urlunparse(parsed._replace(query=new_query, fragment=new_fragment))


def _redact_qs(qs: str) -> str:
    """Redact sensitive keys from a query-string-formatted string.

    Used for both URL query and URL fragment (OAuth implicit flow puts
    tokens in fragments using the same `key=value&...` format).
    """
    pairs = parse_qsl(qs, keep_blank_values=True)
    if not pairs:
        return qs
    redacted_pairs = [(k, REDACTED if k.lower() in DEFAULT_QUERY_KEYS else v) for k, v in pairs]
    return urlencode(redacted_pairs)
