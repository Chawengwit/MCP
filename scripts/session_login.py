"""Operator CLI for the ``session_login`` auth type.

Counterpart to ``scripts/oauth_login.py`` for the OAuth (Phase 3) path.
Where ``oauth_login.py`` drives a browser-based authorization-code flow
for APIs like GitHub, this script handles the simpler "exchange an
api_key + secret_key for a session token" flow used by Service APIs
like Taximail.

Usage:

    # Prompt for credentials interactively
    python -m scripts.session_login taximail

    # Or read from env (useful in scripts / CI)
    TAXIMAIL_API_KEY=...  TAXIMAIL_SECRET_KEY=... \
        python -m scripts.session_login taximail

    # Clear any stored session for an api_id
    python -m scripts.session_login taximail --clear

The api_key + secret_key (and the resulting session_id) are stored in
the OS keyring under the same service name as the GitHub OAuth tokens.
MCP tool calls then read them at request time via
:class:`src.auth.session_login_keyring.KeyringServiceSessionStore`.

Run this once per api_id; the session refreshes silently when it nears
expiry. Re-run only when the operator rotates credentials in the
Service API's own dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

# Reuse the bootstrap so this script can be run from any cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from src.auth.credentials import AuthError, AuthRequiredError  # noqa: E402
from src.auth.service_api import authenticate  # noqa: E402
from src.auth.session_login_keyring import (  # noqa: E402
    KeyringServiceSessionStore,
)
from src.config import load_api_configs  # noqa: E402


def _read_credentials(api_id: str) -> tuple[str, str]:
    """Read api_key + secret_key from env or stdin.

    Env-var names follow the convention ``<API_ID_UPPER>_API_KEY`` and
    ``<API_ID_UPPER>_SECRET_KEY``, e.g. ``TAXIMAIL_API_KEY``.
    """
    env_prefix = api_id.upper()
    api_key = os.environ.get(f"{env_prefix}_API_KEY", "").strip()
    secret_key = os.environ.get(f"{env_prefix}_SECRET_KEY", "").strip()

    if not api_key:
        api_key = input(f"{api_id} api_key: ").strip()
    if not secret_key:
        secret_key = getpass.getpass(f"{api_id} secret_key (hidden): ").strip()

    if not api_key or not secret_key:
        print("api_key and secret_key are both required.", file=sys.stderr)
        sys.exit(2)
    return api_key, secret_key


async def _login(api_id: str, *, clear_first: bool = False) -> int:
    configs = load_api_configs()
    if api_id not in configs:
        print(
            f"'{api_id}' is not in config/api_configs.json. Available: {sorted(configs.keys())}",
            file=sys.stderr,
        )
        return 2

    config = configs[api_id]
    if config.auth is None or (config.auth.type or "").lower() != "session_login":
        print(
            f"'{api_id}' is not a session_login API "
            f"(auth.type={config.auth.type if config.auth else None!r}). "
            "Use scripts/oauth_login.py for oauth2 APIs.",
            file=sys.stderr,
        )
        return 2

    store = KeyringServiceSessionStore.from_configs(configs)

    if clear_first:
        await store.clear(api_id)
        print(f"Cleared stored session for '{api_id}'.")

    api_key, secret_key = _read_credentials(api_id)

    print(f"Authenticating with {api_id}...")
    try:
        session = await authenticate(config, api_key=api_key, secret_key=secret_key)
    except AuthRequiredError as exc:
        print(f"❌ Service API rejected the credentials: {exc}", file=sys.stderr)
        return 1
    except AuthError as exc:
        print(f"❌ Service API error: {exc}", file=sys.stderr)
        return 1

    await store.save(
        api_id=api_id,
        api_key=api_key,
        secret_key=secret_key,
        user_id=session.user_id,
        session_id=session.session_id,
        session_expire=session.session_expire,
        company_group=session.company_group,
        user_type=session.user_type,
        app_package=session.app_package,
    )
    # Print a short, no-secret summary so operators can confirm which
    # account the session belongs to. The session_id itself is never
    # printed — like every other place in this codebase.
    print(f"✓ Stored session for '{api_id}'")
    print(f"  user_id      = {session.user_id}")
    if session.company_group:
        print(f"  company      = {session.company_group}")
    if session.user_type:
        print(f"  user_type    = {session.user_type}")
    if session.app_package:
        print(f"  app_package  = {session.app_package}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate against a session_login Service API and store the session in keyring.",
    )
    parser.add_argument("api_id", help="api_id from config/api_configs.json (e.g. taximail)")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete any existing stored session before logging in.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_login(args.api_id, clear_first=args.clear))


if __name__ == "__main__":
    sys.exit(main())
