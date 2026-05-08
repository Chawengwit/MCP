"""OAuth login helper.

Usage:
    python -m scripts.oauth_login <api_id> [--clear]

Examples:
    python -m scripts.oauth_login github
    python -m scripts.oauth_login github --clear   # delete stored token first

Drives the full OAuth 2.0 + PKCE flow for an API configured in
config/api_configs.json: opens a browser to the provider's authorize URL,
runs a local callback server, exchanges the auth code for tokens, and
stores the result in the system keyring.

After this completes, MCP tools (fetch_data, send_data, execute_graphql)
can use the stored token automatically — including silent refresh near
expiry.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path

# Bootstrap: project root + .env (same pattern as src/server.py)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

from src.auth import Credentials, OAuth, OAuthConfig  # noqa: E402
from src.config import ApiAuthConfig, load_api_configs  # noqa: E402

REQUIRED_OAUTH_FIELDS = (
    "provider",
    "client_id",
    "client_secret",
    "authorize_url",
    "token_url",
    "scopes",
)


class MissingOAuthFieldsError(ValueError):
    """Raised when an ApiAuthConfig lacks required oauth2 fields."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"Missing required oauth2 fields: {missing}")
        self.missing = missing


def _missing_oauth_fields(auth: ApiAuthConfig) -> list[str]:
    """Return the names of required oauth2 fields that are unset/empty."""
    return [f for f in REQUIRED_OAUTH_FIELDS if not getattr(auth, f)]


def _build_oauth_config(auth: ApiAuthConfig) -> OAuthConfig:
    """Map an ApiAuthConfig (config-layer) to an OAuthConfig (auth-layer).

    Raises:
        MissingOAuthFieldsError: if any required field is missing.

    The post-check assertions narrow the optional types for mypy; they
    are guaranteed to hold given the preceding validation.
    """
    missing = _missing_oauth_fields(auth)
    if missing:
        raise MissingOAuthFieldsError(missing)
    assert auth.provider is not None
    assert auth.client_id is not None
    assert auth.client_secret is not None
    assert auth.authorize_url is not None
    assert auth.token_url is not None
    assert auth.scopes is not None
    return OAuthConfig(
        provider=auth.provider,
        client_id=auth.client_id,
        client_secret=auth.client_secret,
        authorize_url=auth.authorize_url,
        token_url=auth.token_url,
        scopes=auth.scopes,
        redirect_uri=auth.redirect_uri,
    )


async def login(api_id: str, *, clear_first: bool = False) -> int:
    """Run the OAuth flow for `api_id` and persist the resulting token.

    Returns:
        0 on success, 1 on any error (config / network / storage).
    """
    configs = load_api_configs()
    if api_id not in configs:
        print(
            f"[ERROR] API '{api_id}' not found in config/api_configs.json",
            file=sys.stderr,
        )
        print(
            f"        Available APIs: {sorted(configs.keys()) or '(none)'}",
            file=sys.stderr,
        )
        return 1

    api_cfg = configs[api_id]
    auth = api_cfg.auth
    if auth is None or auth.type != "oauth2":
        actual = auth.type if auth else "none"
        print(
            f"[ERROR] API '{api_id}' is not configured for oauth2 (auth.type={actual!r})",
            file=sys.stderr,
        )
        return 1

    try:
        oauth_cfg = _build_oauth_config(auth)
    except MissingOAuthFieldsError as exc:
        print(
            f"[ERROR] Missing required oauth2 fields for '{api_id}': {exc.missing}",
            file=sys.stderr,
        )
        print(
            "        Check .env (env var substitution) and config/api_configs.json",
            file=sys.stderr,
        )
        return 1

    oauth = OAuth()
    creds = Credentials(oauth=oauth, oauth_configs={api_id: oauth_cfg})

    if clear_first:
        try:
            await creds.clear(api_id)
            print(f"[oauth_login] Cleared stored credentials for '{api_id}'")
        except Exception as exc:  # noqa: BLE001 — surface-level CLI handler
            print(f"[ERROR] Failed to clear credentials: {exc}", file=sys.stderr)
            return 1

    print(f"[oauth_login] Starting OAuth flow for '{api_id}' (provider={auth.provider})")
    print("[oauth_login] Browser will open. Click 'Authorize' to continue.")
    print(f"[oauth_login] Callback server: http://127.0.0.1:{oauth.callback_port}/callback")
    print()

    try:
        tokens = await oauth.start_flow(oauth_cfg)
    except Exception as exc:  # noqa: BLE001 — surface-level CLI handler
        print(
            f"[ERROR] OAuth flow failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return 1

    try:
        await creds.store(api_id, tokens)
    except Exception as exc:  # noqa: BLE001 — surface-level CLI handler
        print(f"[ERROR] Failed to store credentials: {exc}", file=sys.stderr)
        return 1

    print("[oauth_login] Authenticated successfully")
    print(f"[oauth_login] Token stored in keychain (service=mcp-data-gateway, account={api_id})")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.oauth_login",
        description="Drive an OAuth 2.0 + PKCE login flow for an API in api_configs.json.",
    )
    parser.add_argument("api_id", help="API ID to authenticate (e.g. 'github')")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete any stored credentials for api_id before starting the flow.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(login(args.api_id, clear_first=args.clear))


if __name__ == "__main__":
    sys.exit(main())
