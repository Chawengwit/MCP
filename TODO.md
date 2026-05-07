# TODO — Next Session

> Written 2026-05-07 after Phase 6 ship. Pick up here.

## Recommended order: validate before extending

---

## 1. Validate v0 from Claude Desktop (do this first)

**Goal:** confirm the MCP server speaks correctly to a real Claude client. Catches
MCP SDK quirks / protocol issues that 232 unit tests cannot reach.

- [ ] Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) — add:
  ```json
  {
    "mcpServers": {
      "data-gateway": {
        "command": "/Users/chawengwit/Documents/MCP/.venv/bin/python",
        "args": ["-m", "src.server"],
        "cwd": "/Users/chawengwit/Documents/MCP"
      }
    }
  }
  ```
- [ ] Restart Claude Desktop, look for the 🔌 icon → "data-gateway" listed
- [ ] In a new chat, ask Claude: *"Show me the available APIs"* → should call `list_apis`
- [ ] Ask: *"What's the auth status of each API?"* → should call `get_status`
- [ ] Tail `logs/audit/2026-05.jsonl` — confirm one event per tool call
- [ ] Tail `logs/insight/2026-05.jsonl` — confirm `tool_args` redacted (not that there's much to redact yet)

**If something breaks:** check Claude's MCP log (`~/Library/Logs/Claude/mcp*.log` on
macOS) AND our `logs/debug/*.jsonl` (set `MCP_LOG_DEBUG_ENABLED=true`). Both sides
help.

---

## 2. Add the first real API integration (GitHub OAuth)

**Goal:** end-to-end OAuth flow with a real provider — proves Phase 3 works on the
wire, not just in mocks.

- [ ] Register an OAuth App at https://github.com/settings/developers
  - Application name: `MCP Data Gateway (dev)`
  - Homepage URL: `http://127.0.0.1:8765`
  - Authorization callback URL: `http://127.0.0.1:8765/callback` (must be exactly this — no trailing slash, must use `127.0.0.1` not `localhost`)
- [ ] Add to `.env`:
  ```bash
  GITHUB_CLIENT_ID=<from-github>
  GITHUB_CLIENT_SECRET=<from-github>
  ```
- [ ] Add to `config/api_configs.json` (NOT the example file):
  ```json
  "github": {
    "type": "rest",
    "base_url": "https://api.github.com",
    "auth": {
      "type": "oauth2",
      "provider": "github",
      "client_id": "${GITHUB_CLIENT_ID}",
      "client_secret": "${GITHUB_CLIENT_SECRET}",
      "authorize_url": "https://github.com/login/oauth/authorize",
      "token_url": "https://github.com/login/oauth/access_token",
      "scopes": ["read:user", "public_repo"]
    },
    "endpoints": {
      "get_user": {"method": "GET", "path": "/user"},
      "list_repos": {"method": "GET", "path": "/user/repos", "query_params": ["sort", "per_page"]}
    }
  }
  ```
- [ ] Restart Claude Desktop, ask: *"Fetch my GitHub user profile"* → triggers OAuth popup → authorize → token cached in macOS Keychain
- [ ] Ask again same prompt → no popup (cached token used)
- [ ] Verify token in keyring: `security find-generic-password -s mcp-data-gateway -a github`
- [ ] Test refresh: wait until token expires (or `Credentials.clear("github")`) → next call should auto-refresh silently
- [ ] Confirm `logs/insight/*.jsonl` doesn't contain the access token literal anywhere

**Watch out for:** GitHub returns `expires_in` only for some grant types; check the
TokenInfo `expires_at` is sane. Phase 3 defaults to 3600s if missing.

---

## 3. Polish based on what 1 + 2 surface

(Fill in after Priority 1 & 2 are done. Likely candidates:)

- [ ] Improve a specific error message that confused you
- [ ] Add a `Troubleshooting` row to README for the actual issue you hit
- [ ] Add a second real provider (Google? Slack? Linear?) for variety in auth flows
- [ ] Decide on whether to add response caching (only if real usage shows it's needed)
- [ ] Consider rate-limit handling beyond retry-after (queue-based?) — only if hit in practice

---

## Reference (don't re-read; just here for muscle memory)

- Run tests: `pytest tests/ -q` (should be 232 passing)
- Lint + types: `ruff check src/ tests/ && mypy src/ tests/`
- Smoke test the server: `python -m src.server` then Ctrl-C
- Activity logs: `logs/{audit,debug,usage,insight}/YYYY-MM.jsonl`
- All five tools: `list_apis`, `fetch_data`, `send_data`, `execute_graphql`, `get_status`
- Keyring service name: `mcp-data-gateway`, username = `api_id`

## Status of v0 (committed `dc18cc6`)

All 7 planned phases done. 232 tests passing. README has Quickstart + OAuth setup +
Troubleshooting + Logging sections. Don't re-do any of this — extend.
