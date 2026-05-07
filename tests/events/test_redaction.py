from __future__ import annotations

from src.events.redaction import (
    REDACTED,
    redact_body,
    redact_headers,
    redact_url,
)


def test_redact_headers_lowercases_match() -> None:
    headers = {"Authorization": "Bearer xyz", "X-API-Key": "secret", "Accept": "*/*"}
    result = redact_headers(headers)
    assert result["Authorization"] == REDACTED
    assert result["X-API-Key"] == REDACTED
    assert result["Accept"] == "*/*"


def test_redact_headers_empty() -> None:
    assert redact_headers(None) == {}
    assert redact_headers({}) == {}


def test_redact_body_top_level_keys() -> None:
    body = {
        "username": "alice",
        "password": "hunter2",
        "access_token": "abc",
        "data": {"x": 1},
    }
    result = redact_body(body)
    assert result["username"] == "alice"
    assert result["password"] == REDACTED
    assert result["access_token"] == REDACTED
    assert result["data"] == {"x": 1}


def test_redact_body_nested() -> None:
    body = {
        "auth": {"token": "abc", "user": "alice"},
        "items": [{"api_key": "k1"}, {"api_key": "k2"}],
    }
    result = redact_body(body)
    assert result["auth"]["token"] == REDACTED
    assert result["auth"]["user"] == "alice"
    assert result["items"][0]["api_key"] == REDACTED
    assert result["items"][1]["api_key"] == REDACTED


def test_redact_body_with_extra_keys() -> None:
    body = {"ssn": "123-45-6789", "phone": "555-1234"}
    result = redact_body(body, extra_keys={"ssn"})
    assert result["ssn"] == REDACTED
    assert result["phone"] == "555-1234"


def test_redact_body_case_insensitive() -> None:
    body = {"Password": "x", "TOKEN": "y", "Api_Key": "z"}
    result = redact_body(body)
    assert result["Password"] == REDACTED
    assert result["TOKEN"] == REDACTED
    assert result["Api_Key"] == REDACTED


def test_redact_body_non_dict_passthrough() -> None:
    assert redact_body("plain string") == "plain string"
    assert redact_body(42) == 42
    assert redact_body(None) is None


def test_redact_url_query_params() -> None:
    url = "https://api.example.com/users?api_key=secret&page=1"
    result = redact_url(url)
    assert "api_key=%3Credacted%3E" in result
    assert "page=1" in result


def test_redact_url_no_query() -> None:
    url = "https://api.example.com/users"
    assert redact_url(url) == url


def test_redact_url_fragment_oauth_implicit() -> None:
    """OAuth implicit flow puts tokens in URL fragments (#access_token=...)."""
    url = "https://example.com/callback#access_token=abc&token_type=Bearer&state=xyz"
    result = redact_url(url)
    assert "access_token=%3Credacted%3E" in result
    assert "token_type=Bearer" in result
    assert "state=xyz" in result


def test_redact_url_both_query_and_fragment() -> None:
    url = "https://example.com/cb?api_key=k1&page=1#access_token=t1&user=alice"
    result = redact_url(url)
    assert "api_key=%3Credacted%3E" in result
    assert "page=1" in result
    assert "access_token=%3Credacted%3E" in result
    assert "user=alice" in result
