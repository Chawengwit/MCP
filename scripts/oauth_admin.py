"""Operator CLI for the OAuth Provider (Phase 9).

Subcommands:
  list-clients     — show registered OAuth clients (Dynamic Client Registration)
  list-tokens      — show issued access tokens (masked) per user
  revoke-token     — delete an access token by its (full) value
  list-sessions    — show stored Service API sessions (no plaintext credentials)

The CLI is operator-only and never exposed via MCP. It opens its own
``OAuthStore`` from the env (``MCP_OAUTH_DB_PATH``), so the running
server doesn't need to be paused.

All printable fields are sanitised before output — full opaque tokens
and Service API session IDs are masked (``tok_***xxxx``) so a screen-
share or scrollback never reveals the bearer value that grants
access.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Project-root bootstrap so this script can be invoked from any CWD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

from src.oauth_provider import OAuthStore  # noqa: E402


def _mask(value: str | None, *, keep: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= keep:
        return "***"
    return f"{value[:3]}***{value[-keep:]}"


async def cmd_list_clients(store: OAuthStore) -> int:
    clients = await store.list_clients()
    if not clients:
        print("No clients registered.")
        return 0
    for c in clients:
        print(
            f"client_id={c.client_id}  "
            f"name={c.client_name!r}  "
            f"created_at={c.created_at.isoformat()}  "
            f"redirect_uris={c.redirect_uris}"
        )
    return 0


async def cmd_list_tokens(store: OAuthStore) -> int:
    tokens = await store.list_access_tokens()
    if not tokens:
        print("No access tokens issued.")
        return 0
    for t in tokens:
        print(
            f"token={_mask(t.token)}  "
            f"user_id={t.user_id}  "
            f"client_id={t.client_id}  "
            f"expires_at={t.expires_at.isoformat()}  "
            f"last_used_at={t.last_used_at.isoformat()}  "
            f"refresh_token={_mask(t.refresh_token)}"
        )
    return 0


async def cmd_revoke_token(store: OAuthStore, token: str) -> int:
    if not token:
        print("error: --token is required", file=sys.stderr)
        return 2
    deleted = await store.delete_access_token(token)
    if deleted:
        print("Revoked.")
        return 0
    print("Token not found (already revoked or never existed).", file=sys.stderr)
    return 1


async def cmd_list_sessions(store: OAuthStore) -> int:
    sessions = await store.list_service_sessions()
    if not sessions:
        print("No Service API sessions stored.")
        return 0
    for s in sessions:
        print(
            f"user_id={s.user_id}  "
            f"company_group={s.company_group or '—'}  "
            f"session_id={_mask(s.session_id)}  "
            f"session_expire={s.session_expire}  "
            f"user_type={s.user_type or '—'}  "
            f"updated_at={s.updated_at.isoformat()}"
        )
    return 0


async def main_async(args: argparse.Namespace) -> int:
    store = OAuthStore.from_env()
    await store.init_db()
    if args.command == "list-clients":
        return await cmd_list_clients(store)
    if args.command == "list-tokens":
        return await cmd_list_tokens(store)
    if args.command == "revoke-token":
        return await cmd_revoke_token(store, args.token)
    if args.command == "list-sessions":
        return await cmd_list_sessions(store)
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oauth_admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-clients", help="List registered OAuth clients.")
    sub.add_parser("list-tokens", help="List issued access tokens (masked).")
    revoke = sub.add_parser("revoke-token", help="Delete an access token.")
    revoke.add_argument("--token", required=True, help="Full access token value.")
    sub.add_parser("list-sessions", help="List Service API sessions (masked).")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
