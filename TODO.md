# TODO — Resume Phase 8 + Codex Desktop integration

> Snapshot 2026-05-08, end of session. Pick up here.
>
> See [INITIAL.md](INITIAL.md) for project-level feature tracking.
> This file tracks operational state — what's wired up where, what was last
> verified, and what's left to test.

---

## ✅ Done today

- Phase 8 (HTTP transport) shipped, committed (`1dafca7`), and pushed to `main`
- Server boots in HTTP mode end-to-end (`MCP_TRANSPORT=http python -m src.server`)
- Streamable HTTP wire format verified via `curl` (Phase A: 401 / 200 / 404 + Mcp-Session-Id)
- **Codex Desktop (macOS app, NOT VSCode extension)** integrated with our HTTP server
- 4 real tool calls from Codex Desktop landed in `logs/audit/2026-05.jsonl`:
  - `list_apis` × 2 (success)
  - `get_status(github)` (success — reported `expired`)
  - `fetch_data(github, get_user)` (correctly returned `AUTH_REQUIRED` because token expired)

## 🔧 Current state of moving parts

| Component | State |
|-----------|-------|
| `~/.codex/config.toml` | Has `[mcp_servers.data-gateway-http]` block with `url` + inlined `[mcp_servers.data-gateway-http.http_headers]` `Authorization = "Bearer …"` |
| Bearer token in Codex config | `1d77a585694dd513bfa2ec4bf06618124f75d5eb1f8f8082b3316c1937fe4a42` (64-char hex) |
| Background HTTP server | **STOPPED** (was task `b4k3vbftc`, killed via SIGTERM) |
| GitHub OAuth token in keychain | **EXPIRED** (1h default lifetime; expired ~07:16 UTC, last verified ~11:52 UTC) |
| stdio transport for Claude Desktop | Still wired up at `~/Library/Application Support/Claude/claude_desktop_config.json` — works untouched |

> **Why inlined header instead of `bearer_token_env_var`?**
> Codex Desktop runs as a Mac app; its process tree doesn't inherit env vars from
> the shell unless launched from one. The UI's "Bearer token env var" field
> couldn't see `MCP_HTTP_BEARER_TOKEN` from a generic terminal. Inlining the
> header in `http_headers` is the workaround that ships the value through Codex
> Desktop's own config storage.

## 🚀 To resume tomorrow

### Option 1 — Continue HTTP integration (if you want full chain proven)

```bash
# 1. Re-auth GitHub (the in-keychain token is expired)
cd /Users/chawengwit/Documents/MCP
.venv/bin/python -m scripts.oauth_login github --clear

# 2. Start HTTP server in one terminal (keep it running)
MCP_TRANSPORT=http \
MCP_HTTP_BEARER_TOKEN=1d77a585694dd513bfa2ec4bf06618124f75d5eb1f8f8082b3316c1937fe4a42 \
python -m src.server
```

> NOTE: The bearer token here must match the value already inlined in
> `~/.codex/config.toml`. If you regenerate the token, update both places.

Then in **Codex Desktop**, ask:
> *"Fetch my GitHub user profile via data-gateway-http"*

Expect: real GitHub profile (`login: Chawengwit`) returned — this proves the full
chain Codex → HTTP transport → REST gateway → GitHub API → response.

### Option 2 — Document Codex Desktop setup in README

Codex Desktop's `http_headers` workaround is non-obvious and worth committing as
a `README.md` recipe so anyone reading the repo can replicate. Suggested location:
under "HTTP transport" section, a new subsection **"Connecting from Codex Desktop"**.

Outline:
```markdown
### Connecting from Codex Desktop (macOS app)

The Codex Desktop app's UI accepts a `bearer_token_env_var`, but env vars set in
your shell aren't visible to apps launched outside that shell. The cleanest
workaround is to inline the token as a static header in `~/.codex/config.toml`:

```toml
[mcp_servers.data-gateway-http]
url = "http://127.0.0.1:8080/mcp"

[mcp_servers.data-gateway-http.http_headers]
Authorization = "Bearer <your-token>"
```

After editing, fully quit (Cmd+Q) and relaunch Codex Desktop.
```

### Option 3 — Rotate the token

The token has been visible in this conversation log. For ongoing dev OK; for
production, regenerate:

```bash
NEW=$(python -c 'import secrets; print(secrets.token_hex(32))')
echo "$NEW"
# update ~/.codex/config.toml http_headers entry
# restart server with MCP_HTTP_BEARER_TOKEN=$NEW
```

## 🧹 Cleanup if you want to stop using HTTP transport entirely

- Comment out or delete the `[mcp_servers.data-gateway-http]` block in
  `~/.codex/config.toml`
- The original stdio `[mcp_servers.data-gateway]` entry was removed earlier in
  the session (no longer present); add it back if you want stdio in Codex
  Desktop:

  ```toml
  [mcp_servers.data-gateway]
  command = "/Users/chawengwit/Documents/MCP/.venv/bin/python"
  args = ["/Users/chawengwit/Documents/MCP/src/server.py"]
  ```

## 📊 Session checkpoints

- Test count: **288 passing** (Phase 8 + cleanup)
- Last commit: `1dafca7 Add HTTP transport (Phase 8) with operator-driven OAuth model`
- Branch: `main` (pushed to `origin`)
- Working tree: clean

## 🔍 Reference (muscle memory)

- Run tests: `pytest tests/ -q` (288 passing)
- Lint + types: `ruff check src/ scripts/ tests/ && mypy src/ scripts/ tests/`
- Smoke (stdio): `python -m src.server` then Ctrl-C
- Smoke (http): `MCP_TRANSPORT=http MCP_HTTP_BEARER_TOKEN=... python -m src.server`
- OAuth login (operator CLI): `python -m scripts.oauth_login github [--clear]`
- Activity logs: `logs/{audit,debug,usage,insight}/YYYY-MM.jsonl`
- All five tools: `list_apis`, `fetch_data`, `send_data`, `execute_graphql`, `get_status`
- Keyring service: `mcp-data-gateway`, account = `api_id`
- HTTP endpoint: `POST /mcp` on `127.0.0.1:8080` (default), Bearer auth required
