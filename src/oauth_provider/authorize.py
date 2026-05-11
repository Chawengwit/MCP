"""GET /authorize + POST /authorize/consent.

Two handlers backing the OAuth authorization-code-with-PKCE flow:

- ``GET /authorize`` validates the protocol params and renders a small
  HTML form that asks the user for their Service API ``api_key`` /
  ``secret_key``. The form's hidden fields carry the protocol params
  through to the POST.
- ``POST /authorize/consent`` calls
  :func:`src.auth.service_api.authenticate`, stores the encrypted
  Service API session under the resolved ``user_id``, generates a
  short-lived authorization code, and 302-redirects to
  ``redirect_uri?code=&state=``.

If the Service API rejects the supplied credentials we re-render the
form with a sanitised error banner. Stack traces / upstream bodies are
never surfaced to the browser — that path leaks internals.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from pydantic import ValidationError
from starlette.types import Receive, Scope, Send

from src.auth.credentials import AuthError, AuthRequiredError
from src.config import ApiConfig

from .pkce import SUPPORTED_METHODS
from .schemas import ConsentForm, SessionInfo
from .service_session import ServiceAuthCallable, ServiceSessionStore
from .store import OAuthStore

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "consent.html"
_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")

REQUIRED_AUTHORIZE_PARAMS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "state",
)


# ----------------------------------------------------------------------
# GET /authorize
# ----------------------------------------------------------------------


async def authorize_get_handler(
    *,
    scope: Scope,
    receive: Receive,
    send: Send,
    store: OAuthStore,
) -> None:
    if scope.get("method", "") != "GET":
        await _send_error(send, 405, "method_not_allowed", "Use GET.")
        return

    params = _parse_query(scope)

    validation = await _validate_authorize_params(params, store)
    if isinstance(validation, _AuthorizeError):
        await validation.send(send)
        return

    # Validation succeeded — look up the client_name for the header.
    client = await store.get_client(params["client_id"])
    if client is not None:
        params["_client_name"] = client.client_name

    page = _render_consent(params, error=None)
    await _send_html(send, 200, page)


# ----------------------------------------------------------------------
# POST /authorize/consent
# ----------------------------------------------------------------------


async def consent_post_handler(
    *,
    scope: Scope,
    receive: Receive,
    send: Send,
    store: OAuthStore,
    service_session_store: ServiceSessionStore,
    authenticate: ServiceAuthCallable,
    api_config: ApiConfig,
) -> None:
    if scope.get("method", "") != "POST":
        await _send_error(send, 405, "method_not_allowed", "Use POST.")
        return

    body_bytes = await _read_body(receive)
    form = _parse_form(body_bytes)

    try:
        # Pydantic v2 narrows the dict-spread to the model's literal fields at
        # validation time; mypy can't see that through `dict[str, str]`.
        consent = ConsentForm.model_validate(form)
    except ValidationError:
        await _send_error(send, 400, "invalid_request", "Form is missing required fields.")
        return

    # Re-run the same validation as GET — defends against a tampered POST
    # body sneaking in a redirect_uri the registered client doesn't own.
    params_for_validation = {
        "client_id": consent.client_id,
        "redirect_uri": consent.redirect_uri,
        "response_type": consent.response_type,
        "code_challenge": consent.code_challenge,
        "code_challenge_method": consent.code_challenge_method,
        "state": consent.state,
    }
    validation = await _validate_authorize_params(params_for_validation, store)
    if isinstance(validation, _AuthorizeError):
        await validation.send(send)
        return

    # Annotate with client_name for the consent header (look up once,
    # reuse on every error-rerender below).
    client = await store.get_client(consent.client_id)
    if client is not None:
        params_for_validation["_client_name"] = client.client_name

    # Authenticate against the Service API. Failures re-render the form.
    try:
        session: SessionInfo = await authenticate(
            api_config,
            api_key=consent.api_key,
            secret_key=consent.secret_key,
        )
    except AuthRequiredError:
        page = _render_consent(
            params_for_validation,
            error="Service API credentials were rejected. Check the values and try again.",
        )
        await _send_html(send, 401, page)
        return
    except AuthError:
        page = _render_consent(
            params_for_validation,
            error="Could not complete sign-in with the Service API. Please retry.",
        )
        await _send_html(send, 502, page)
        return

    await service_session_store.save(
        user_id=session.user_id,
        api_key=consent.api_key,
        secret_key=consent.secret_key,
        session=session,
    )

    code = await store.save_authorization_code(
        client_id=consent.client_id,
        user_id=session.user_id,
        redirect_uri=consent.redirect_uri,
        code_challenge=consent.code_challenge,
    )

    redirect = _build_redirect(
        redirect_uri=consent.redirect_uri,
        code=code.code,
        state=consent.state,
    )
    await _send_redirect(send, redirect)


# ----------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------


class _AuthorizeError:
    """Internal carrier for a validation failure during /authorize."""

    def __init__(
        self,
        *,
        status: int,
        error: str,
        description: str,
        redirect_uri: str | None = None,
        state: str | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.description = description
        self.redirect_uri = redirect_uri
        self.state = state

    async def send(self, send: Send) -> None:
        # Per RFC 6749 §4.1.2.1: if redirect_uri and client_id are both
        # valid we return the error via redirect; otherwise we render
        # locally so the user is not bounced to an attacker-controlled
        # URL with our error message.
        if self.redirect_uri is not None:
            qs = {"error": self.error, "error_description": self.description}
            if self.state is not None:
                qs["state"] = self.state
            target = self.redirect_uri
            sep = "&" if ("?" in target) else "?"
            await _send_redirect(send, f"{target}{sep}{urlencode(qs)}")
            return
        await _send_error(send, self.status, self.error, self.description)


async def _validate_authorize_params(
    params: dict[str, str],
    store: OAuthStore,
) -> dict[str, str] | _AuthorizeError:
    for key in REQUIRED_AUTHORIZE_PARAMS:
        value = params.get(key, "")
        if not value:
            return _AuthorizeError(
                status=400,
                error="invalid_request",
                description=f"Missing required parameter: {key}",
            )

    if params["response_type"] != "code":
        return _AuthorizeError(
            status=400,
            error="unsupported_response_type",
            description="Only response_type=code is supported.",
        )

    if params["code_challenge_method"] not in SUPPORTED_METHODS:
        return _AuthorizeError(
            status=400,
            error="invalid_request",
            description="code_challenge_method must be S256.",
        )

    client = await store.get_client(params["client_id"])
    if client is None:
        return _AuthorizeError(
            status=400,
            error="invalid_client",
            description="Unknown client_id.",
        )

    if params["redirect_uri"] not in client.redirect_uris:
        # Defence-in-depth: an attacker who guesses a client_id cannot
        # redirect tokens to an arbitrary URL. Literal-byte comparison
        # post %-decode (Starlette already %-decodes the query string).
        return _AuthorizeError(
            status=400,
            error="invalid_request",
            description="redirect_uri is not registered for this client.",
        )

    return params


# ----------------------------------------------------------------------
# I/O helpers
# ----------------------------------------------------------------------


def _parse_query(scope: Scope) -> dict[str, str]:
    raw_qs = scope.get("query_string", b"")
    if isinstance(raw_qs, str):  # pragma: no cover — ASGI sends bytes
        raw_qs = raw_qs.encode("latin-1")
    return dict(parse_qsl(raw_qs.decode("latin-1"), keep_blank_values=True))


def _parse_form(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        more = bool(message.get("more_body", False))
    return b"".join(chunks)


def _render_consent(params: dict[str, str], *, error: str | None) -> str:
    """Render the consent template with html-escaped values.

    Every value substituted into the template is run through
    ``html.escape`` — the attacker can control ``redirect_uri``,
    ``state``, and any of the protocol parameters they POST, and a
    careless interpolation here is an XSS sink.
    """
    error_block = ""
    if error:
        error_block = f'<div class="error">{html.escape(error)}</div>'

    # client_name lives on the registered client, not the URL — look it
    # up here so we can show a friendly label rather than a random ID.
    # We've already validated the client exists at this point in flow.
    client_name = params.get("_client_name", "MCP Client")

    return _TEMPLATE.format(
        client_name=html.escape(client_name),
        client_id=html.escape(params["client_id"]),
        redirect_uri=html.escape(params["redirect_uri"]),
        state=html.escape(params["state"]),
        code_challenge=html.escape(params["code_challenge"]),
        code_challenge_method=html.escape(params["code_challenge_method"]),
        error_block=error_block,
    )


def _build_redirect(*, redirect_uri: str, code: str, state: str) -> str:
    parsed = urlparse(redirect_uri)
    qs = urlencode({"code": code, "state": state}, quote_via=quote)
    if parsed.query:
        new_query = f"{parsed.query}&{qs}"
    else:
        new_query = qs
    return parsed._replace(query=new_query).geturl()


async def _send_html(send: Send, status: int, body: str) -> None:
    payload = body.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _send_redirect(send: Send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 302,
            "headers": [
                (b"location", location.encode("utf-8")),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _send_error(send: Send, status: int, code: str, description: str) -> None:
    body = json.dumps({"error": code, "error_description": description}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ----------------------------------------------------------------------
# Helpers shared with tests
# ----------------------------------------------------------------------


def render_consent_page(*, params: dict[str, str], error: str | None = None) -> str:
    """Public helper used by tests to inspect the rendered HTML."""
    return _render_consent(params, error=error)


def expose_template(**overrides: Any) -> str:
    """Test helper for raw template introspection."""
    return _TEMPLATE.format(**overrides)
