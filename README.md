# MCP Data Gateway

> **Status: stdio + HTTP transports live.** All seven planned phases plus the GitHub OAuth integration plus Phase 8 (HTTP transport) are shipped. Selectable via `MCP_TRANSPORT={stdio,http}` (stdio default). Verified against **Claude Desktop**, **OpenAI Codex CLI** (stdio), and the Streamable HTTP test suite. **288 passing tests**. See the [Development Roadmap](#development-roadmap) or [`docs/plan.md`](docs/plan.md).

A Python-based **Model Context Protocol (MCP) server** that acts as a unified data gateway, enabling Claude (and other MCP clients) to send and receive data across multiple external APIs through a single, secure interface.

## Overview

This MCP server provides:
- **Generic data handling** for multiple data types
- **Generic API gateway** supporting any REST or GraphQL endpoint
- **OAuth 2.0 authentication** via an operator-run `scripts/oauth_login.py` (PKCE flow, `127.0.0.1` callback)
- **Secure credential storage** using system keyring
- **Foundation for MCP App** evolution in the future

## Features

| Feature | Description |
|---------|-------------|
| Multi-API Support | Connect to any number of external services through unified configuration |
| REST + GraphQL | Native support for both REST and GraphQL APIs |
| OAuth 2.0 | Full authorization code flow + PKCE, run once via `scripts/oauth_login.py` |
| Token Refresh | Silent refresh inside `Credentials.get()` when ≤ 5 min from expiry |
| Secure Storage | Credentials stored in OS keyring (Keychain / Credential Manager / Secret Service) |
| Generic Data Models | Flexible schemas to handle any data shape |
| Predictable Auth Surface | Tools return `AUTH_REQUIRED` when no token; operator runs `scripts/oauth_login.py` to obtain one |

## Architecture

Files marked **(implemented ✓)** exist today. Files marked **(planned)** are upcoming phases.

```
MCP/
├── src/
│   ├── server.py              # MCP server entry point (implemented ✓)
│   ├── auth/                  # OAuth 2.0 + keyring (implemented ✓)
│   │   ├── oauth.py           # PKCE auth-code flow, callback server bound to 127.0.0.1
│   │   └── credentials.py     # Keyring-backed store with peek/get/store/clear
│   ├── gateway/               # REST/GraphQL HTTP client (implemented ✓)
│   │   ├── api_client.py      # RestClient + GraphQLClient with retry, redacted logging
│   │   └── handlers.py        # Response normalization, size enforcement, error mapping
│   ├── models/                # Pydantic data models (planned)
│   │   └── data_models.py
│   ├── tools/                 # MCP tool definitions (implemented ✓)
│   │   ├── builtin.py         # list_apis tool
│   │   ├── registry.py        # ToolRegistry / ToolSpec
│   │   ├── context.py         # ToolContext dependency container
│   │   ├── auth_resolver.py   # auth.type branching (oauth2/bearer/api_key/None)
│   │   └── mcp_tools.py       # fetch_data/send_data/execute_graphql/get_status
│   ├── config.py              # API config loader with ${VAR} substitution (implemented ✓)
│   ├── events/                # Activity logging (implemented ✓)
│   │   ├── schemas.py         # Pydantic models (audit/debug/usage/insight)
│   │   ├── redaction.py       # Sensitive data redaction
│   │   ├── retention.py       # Per-month file rotation cleanup
│   │   ├── writers.py         # Async JSONL writer + queue
│   │   └── recorder.py        # Public Recorder API
│   └── transport/             # Transport layer (Phase 8 — implemented ✓)
│       ├── stdio.py           # stdio transport (default)
│       └── http.py            # Streamable HTTP + Bearer middleware + loopback guard
├── config/
│   ├── api_configs.json       # API service configurations (committed; uses ${VAR} placeholders)
│   └── api_configs.example.json  # Template with all four auth.type variants
├── docs/
│   └── plan.md                # Implementation plan / roadmap
├── scripts/                   # Operator CLI helpers (implemented ✓)
│   └── oauth_login.py         # Drive OAuth 2.0 + PKCE flow, persist token in keyring
├── tests/
│   ├── auth/                  # Unit tests for src/auth/ (49 cases — implemented ✓)
│   ├── events/                # Unit tests for src/events/ (27 cases, 51 collected w/ parametrize)
│   ├── gateway/               # Unit tests for src/gateway/ (61 cases — implemented ✓)
│   ├── tools/                 # Unit tests for src/tools/ (37 cases — implemented ✓)
│   ├── scripts/               # Unit tests for scripts/ (15 cases — implemented ✓)
│   ├── transport/             # Unit tests for src/transport/ (23 cases — implemented ✓)
│   ├── integration/           # Full-flow + subprocess smoke tests (5 cases — implemented ✓)
│   ├── test_config.py         # Config loader tests
│   ├── test_server.py         # Server bootstrap + _build_oauth_configs tests
│   └── test_example_config.py # Schema-drift guard for api_configs.example.json
├── .claude/commands/          # Slash commands for the dev workflow
│   ├── generate-prp.md        #   /generate-prp INITIAL.md  → PRPs/{feature}.md
│   └── execute-prp.md         #   /execute-prp PRPs/{...}   → implements + validates
├── PRPs/
│   ├── templates/prp_base.md  # Template each PRP fills in
│   └── {feature}.md           # Generated implementation blueprints
├── INITIAL.md                 # Per-feature scope delta (input to /generate-prp)
├── .env.example               # Environment variables template
├── .gitignore                 # Excludes secrets and build artifacts
├── pyproject.toml             # pytest + ruff + mypy configuration
├── requirements.txt           # Runtime dependencies
├── requirements-dev.txt       # Dev/test deps (pytest, ruff, mypy)
├── CLAUDE.md                  # Project rules + Context Engineering workflow
└── README.md                  # This file
```

### MCP Tools

| Tool | Description |
|------|-------------|
| `fetch_data` | GET data from a configured API (auto-OAuth if required) |
| `send_data` | POST/PUT data to a configured API (auto-OAuth if required) |
| `execute_graphql` | Run a GraphQL query or mutation (auto-OAuth if required) |
| `list_apis` | List all configured API services |
| `get_status` | Show authentication and connection status |

Per-module responsibilities and detailed module-by-module breakdown:
[`docs/plan.md` § Architecture Overview](docs/plan.md). Activity logging contract
(four categories, retention, redaction): [`CLAUDE.md` § Activity Logging](CLAUDE.md).

## Authentication Flow

OAuth login is **operator-initiated** (one-time, per provider) and tools are
**read-only with respect to credential acquisition** — they never auto-open
the browser. This split keeps the request path predictable for clients
(Claude Desktop, Codex CLI, etc.) and concentrates the browser flow in a
single CLI surface that's easy to script and test.

```
A) First-time login (operator runs once per provider)
─────────────────────────────────────────────────────
$ python -m scripts.oauth_login github
        ↓
1. Script builds authorize URL with PKCE
2. Browser opens at provider's authorize page
3. User clicks "Authorize"
4. Callback at http://127.0.0.1:8765/callback receives auth code
5. Script exchanges code for tokens
6. TokenInfo persisted in OS keyring (service=mcp-data-gateway, account=<api_id>)


B) Subsequent tool calls (auto, no UI)
──────────────────────────────────────
1. Claude invokes tool (e.g., fetch_data)
        ↓
2. MCP checks credentials in keyring
        ↓
3a. Valid token                                       →  proceed with API call
3b. Token < 5 min from expiry, refresh_token present  →  silent refresh, then proceed
3c. No token / expired with no refresh                →  return AUTH_REQUIRED *
        ↓
4. Tool returns response (data + metadata, or {error: AUTH_REQUIRED})

* On AUTH_REQUIRED, operator re-runs `python -m scripts.oauth_login <api_id>`
  and then retries the original tool call.
```

## Tech Stack

- **Python 3.10+**
- **mcp** — Model Context Protocol Python SDK
- **httpx** — Async HTTP client (REST + GraphQL)
- **keyring** — Cross-platform secure credential storage
- **pydantic** — Data validation and modeling
- **python-dotenv** — Environment variable management
- **uvicorn + starlette** — ASGI server + framework for the HTTP transport (Phase 8)

## Quickstart

Prerequisites: Python 3.10+ and `pip` (or `uv`).

```bash
# 1. Clone and enter the repo
git clone https://github.com/Chawengwit/MCP.git mcp-data-gateway
cd mcp-data-gateway

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install runtime + dev dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt # only if you want to run pytest

# 4. Copy the env + config templates and edit them
cp .env.example .env                       # add provider client_id / secrets
cp config/api_configs.example.json config/api_configs.json

# 5. Run the server (talks MCP over stdio)
python -m src.server
```

The server boots, loads `config/api_configs.json`, starts the Recorder, builds the
`ToolContext`, and registers all five tools (`list_apis`, `fetch_data`, `send_data`,
`execute_graphql`, `get_status`). On SIGINT/SIGTERM the Recorder queue drains and
the server exits cleanly.

> **First-run note.** With the default example config the server will warn
> *"Skipping OAuth config for example_rest_api: missing client_id, client_secret"*
> until you populate `EXAMPLE_REST_CLIENT_ID` and `EXAMPLE_REST_CLIENT_SECRET` in
> `.env`. The other tools (`list_apis`, `get_status`, plus any `bearer` /
> `api_key` / no-auth APIs) work without OAuth setup.

## Configuring an API

The shipped [`config/api_configs.example.json`](config/api_configs.example.json)
covers all four `auth.type` paths:

| Example entry | `auth.type` | Use when |
|---|---|---|
| `example_rest_api` | `oauth2` | Provider supports OAuth 2.0 auth-code flow (Google, GitHub, custom) |
| `example_graphql_api` | `bearer` | You already have a long-lived token in an env var |
| `example_apikey_api` | `api_key` | Provider uses a static key in a custom header |
| `public_no_auth_api` | `null` | Public endpoints with no auth |

**Never commit literal secrets** — every credential field in the file uses a
`${ENV_VAR}` placeholder. The config loader substitutes from your `.env` (or the
process environment) at startup.

### Environment variables

See [`.env.example`](.env.example) for the full annotated template. Most-used
variables:

| Var | Default | What it does |
|---|---|---|
| `MCP_API_CONFIG_PATH` | `config/api_configs.json` | Override the API config file path (useful for XDG_CONFIG_HOME, per-environment configs, or CI isolation) |
| `OAUTH_CALLBACK_PORT` | `8765` | Port the OAuth callback HTTP server binds to |
| `MCP_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARN` / `ERROR` |
| `MCP_LOG_DIR` | `./logs` | Where activity-log JSONL files go |
| `MCP_LOG_RETENTION_DAYS` | `365` | Older monthly files are pruned at startup |
| `MCP_LOG_DEBUG_ENABLED` | `true` | Toggles per-request HTTP debug events |
| `MCP_MAX_RESPONSE_BYTES` | `10485760` (10 MiB) | Response-size cap before truncation / `RESPONSE_TOO_LARGE` |
| `MCP_REQUEST_TIMEOUT_SEC` | `30` | Per-request timeout for outbound HTTP |

## Usage

### Running Tests

```bash
# After installing requirements-dev.txt:
pytest tests/

# Run a specific test file with verbose output
pytest tests/events/test_writers.py -v

# Currently 288 tests passing across src/events/, src/auth/, src/gateway/, src/tools/, src/transport/, scripts/, plus integration + example-config drift tests.
```

### Running the MCP Server

```bash
python -m src.server
```

The server boots, loads `config/api_configs.json`, starts the Recorder, builds the
`ToolContext`, and registers all five tools (`list_apis`, `fetch_data`, `send_data`,
`execute_graphql`, `get_status`). On SIGINT/SIGTERM the Recorder queue drains and
the server exits cleanly.

### Connecting to Claude Desktop

`server.py` performs its own bootstrap (`sys.path` + `os.chdir` + `load_dotenv`),
so a `cwd` field in the client config is not required — point `args` at the
script directly:

```json
{
  "mcpServers": {
    "data-gateway": {
      "command": "/path/to/mcp-data-gateway/.venv/bin/python",
      "args": ["/path/to/mcp-data-gateway/src/server.py"]
    }
  }
}
```

On macOS, this lives at
`~/Library/Application Support/Claude/claude_desktop_config.json`.
After editing, fully quit and relaunch Claude Desktop; the 🔌 menu should list
`data-gateway` with all five tools.

### Connecting to OpenAI Codex CLI

Codex CLI uses TOML at `~/.codex/config.toml`:

```toml
[mcp_servers.data-gateway]
command = "/path/to/mcp-data-gateway/.venv/bin/python"
args = ["/path/to/mcp-data-gateway/src/server.py"]
```

Verified against `codex` CLI — `list_apis`, `get_status`, and `fetch_data`
round-trip identically to Claude Desktop.

### Other MCP clients (Cursor, Cline, etc.)

Any client that supports stdio MCP servers will work with the same shape: a
command and an args list.

### HTTP transport (ChatGPT Connectors, MCP Inspector, custom HTTP clients)

Set `MCP_TRANSPORT=http` to expose the server as a Streamable HTTP endpoint
instead of stdio.

```bash
# Generate a strong bearer token
export MCP_HTTP_BEARER_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# Run on loopback (default host/port)
MCP_TRANSPORT=http \
MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8080 \
python -m src.server
```

The server prints its bind address to stderr and serves the MCP protocol at
`POST /mcp`, `GET /mcp`, and `DELETE /mcp`.

**Test from curl:**

```bash
# Without the token → 401
curl -i -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'

# With the token → initializes a session
curl -i -X POST http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer $MCP_HTTP_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-11-25","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1.0"}}}'
```

The response includes a `Mcp-Session-Id` header — echo it on every subsequent
request via `-H "mcp-session-id: <value>"`.

**Connect from MCP Inspector** (great for interactive debugging):

```bash
npx @modelcontextprotocol/inspector
# UI opens at http://localhost:6274
# Transport: Streamable HTTP
# URL:       http://127.0.0.1:8080/mcp
# Auth:      Bearer <your token>
```

**Connect from ChatGPT Custom Connectors**: paste the public URL of the server
(via reverse proxy / Cloudflare Tunnel / ngrok) and the bearer token. ChatGPT
talks server-to-server, so no CORS configuration is needed.

**Loopback safety guard.** The server **refuses to start** if `MCP_HTTP_HOST`
is non-loopback (`0.0.0.0`, public IP, etc.) and `MCP_HTTP_BEARER_TOKEN` is
unset or empty — preventing accidental unauthenticated public binds. Set the
token, bind to `127.0.0.1`, or enable the OAuth Provider (see below).

### OAuth Provider — Claude.ai "Add custom connector" (Phase 9)

When `MCP_OAUTH_ENCRYPTION_KEY` is set, the HTTP transport additionally exposes
an OAuth 2.0 Authorization Server so Claude.ai (and any MCP client that speaks
OAuth 2.1 + RFC 9728) can connect.

```bash
# 1. Generate a Fernet key once and add it to .env. Rotating it later invalidates
#    every stored Service API session — keep it stable.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Configure the Service API entry in config/api_configs.json with
#    auth.type=session_login. See config/api_configs.example.json for the shape.

# 3. Start the server with OAuth Provider on (loopback or ngrok-tunnelled HTTPS).
MCP_TRANSPORT=http \
MCP_OAUTH_ENCRYPTION_KEY=<paste fernet key> \
MCP_OAUTH_ISSUER=https://<your-ngrok-host>.ngrok-free.app \
python -m src.server

# 4. In Claude.ai, "Add custom connector" → paste the issuer URL.
#    Claude.ai will discover /.well-known/oauth-authorization-server,
#    POST /register, then redirect users to /authorize. Each user pastes
#    their Service API api_key + secret_key into the consent form;
#    the encrypted session is stored under their Service API user_id.
```

Operator inspection (no plaintext output):

```bash
python -m scripts.oauth_admin list-clients
python -m scripts.oauth_admin list-tokens
python -m scripts.oauth_admin list-sessions
python -m scripts.oauth_admin revoke-token --token <full opaque token>
```

The Phase 8 static-bearer path is unchanged when the OAuth Provider is on; both
auth methods are accepted at `/mcp`. OAuth tokens carry a `user_id` into
audit/usage/insight logs, static-bearer tokens carry `user_id=null`.

### Example Interactions

Once connected, Claude can:

- **List configured APIs**: "Show me the available API services"
- **Fetch data**: "Get the user list from example_api"
- **Send data**: "Create a new record in example_api with this data..."
- **Execute GraphQL**: "Run this GraphQL query against my API..."

The first time Claude uses a tool requiring authentication, your browser will open automatically for OAuth login.

## Response Format

All MCP tools return structured JSON: `{data, metadata}` on success, `{error}` on failure.
Large responses truncate (success + cursor) where safe; binary/streaming emit
`RESPONSE_TOO_LARGE`. GraphQL surfaces partial-success (data + errors).

For the full spec — exact field shapes, the error-code table, the truncation rule, and
the GraphQL handling — see
[`CLAUDE.md` § Response Format Conventions](CLAUDE.md).

## OAuth Setup

For each `oauth2` API in `api_configs.json`:

1. **Register an OAuth application** with the provider (GitHub OAuth Apps,
   Google Cloud OAuth client, etc.) and set the **redirect URI** to
   `http://127.0.0.1:8765/callback` exactly. Use `127.0.0.1`, not `localhost` —
   browsers may treat them as different origins for OAuth state tracking.
2. Copy the resulting **client ID** and **client secret** into `.env` under
   names matching your `api_configs.json` placeholders. By convention:
   `${API_ID_UPPER}_CLIENT_ID` and `${API_ID_UPPER}_CLIENT_SECRET` — e.g.
   `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` for `api_id="github"`.
3. **Run the operator login helper once per provider:**
   ```bash
   python -m scripts.oauth_login github            # initial login
   python -m scripts.oauth_login github --clear    # delete + re-auth
   ```
   The script opens your browser at the provider's authorize URL, captures
   the auth code via the one-shot `127.0.0.1` callback server, exchanges it
   for tokens, and stores them in your OS keyring. Subsequent runs reuse the
   stored token; the gateway auto-refreshes when ≤ 5 minutes remain.

> **Why a separate script?** MCP tools (`fetch_data`, `send_data`,
> `execute_graphql`) intentionally **do not** auto-trigger the browser flow —
> they return `AUTH_REQUIRED` so the client (Claude / Codex / etc.) can
> surface it cleanly. The login script is the operator-side counterpart.

If the default port `8765` is in use on your machine, set
`OAUTH_CALLBACK_PORT=<free-port>` in `.env` *and* update the redirect URI you
registered with the provider.

## Keyring Setup (per OS)

Tokens are stored in the OS-native secure keyring. No additional setup is
needed on most desktops:

| OS | Backend | Action required |
|---|---|---|
| **macOS** | Keychain | None — works out of the box |
| **Windows** | Credential Manager | None — works out of the box |
| **Linux (desktop)** | Secret Service (gnome-keyring / KWallet) | Ensure your session has one running (most distros do) |
| **Linux (headless / CI / Docker)** | None by default | `pip install keyrings.alt` for a file backend, OR provide tokens via `bearer` auth (`token_env`) and skip OAuth |

If keyring is unavailable, `Credentials` raises
`CredentialStorageError("No keyring backend available. Install 'keyrings.alt' …")`
on the first OAuth API call. The error is fail-loud by design — the gateway
will not silently fall back to a less secure store.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Server logs *"Skipping OAuth config for X: missing client_id, client_secret"* | `.env` placeholders not set | Populate the matching `${VAR}` in `.env`, restart |
| OAuth tool hangs after the browser popup | `OAUTH_CALLBACK_PORT` blocked or in use | Free the port or set a different one in `.env`; update the provider's redirect URI to match |
| OAuth callback returns 400 with *"State parameter mismatch"* | Browser opened a stale tab from a previous flow | Close all browser tabs pointing at `127.0.0.1:8765`, retry |
| `CredentialStorageError: No keyring backend available` | Headless Linux without DBus / Secret Service | `pip install keyrings.alt`, OR use bearer-token auth |
| Tool returns `RESPONSE_TOO_LARGE` on a binary download | Hit `MCP_MAX_RESPONSE_BYTES` cap (binary cannot be safely truncated) | Raise the cap, or use the API's pagination, or stream out-of-band |
| GraphQL response shows both `data` and `errors` | **Intentional** — GraphQL allows partial success; both are surfaced to Claude | Not a bug. See [CLAUDE.md § GraphQL Specifics](CLAUDE.md) |
| Server smoke test fails with *"Server exited before ready marker"* | A required env var (`MCP_LOG_DIR` etc.) is unset or points to an unwritable path | Check stderr for the actual exception; fix permissions or the var |
| 401 returned after the tool call worked previously | Refresh token expired or revoked at the provider | Delete the keyring entry (or call `Credentials.clear(api_id)`) and re-auth |
| Module import error: `ModuleNotFoundError: keyrings.alt` (Linux only) | Tried to use the `keyrings.alt` fallback but didn't install it | `pip install keyrings.alt` |

For the full failure-modes table that this section is derived from, see
[`CLAUDE.md` § Common Failure Modes](CLAUDE.md).

## Logging (Operator-Only)

The Recorder writes one append-only JSONL file per **(category, month)** under
`$MCP_LOG_DIR` (default `./logs/`):

```
logs/
├── audit/   YYYY-MM.jsonl   who/when/what — security & compliance
├── debug/   YYYY-MM.jsonl   full HTTP exchange (redacted) — troubleshooting
├── usage/   YYYY-MM.jsonl   per-call latency / sizes — analytics
└── insight/ YYYY-MM.jsonl   tool args + response summaries — Claude's request patterns
```

- Rotation is automatic per month; the **current month is never deleted**.
- Old files are pruned on server startup according to
  `MCP_LOG_RETENTION_DAYS` (default 365).
- All four streams pass through the central redaction helpers
  (`src/events/redaction.py`); `Authorization` headers, `access_token`,
  `refresh_token`, `client_secret`, `password`, `api_key`, `secret`, and any
  per-API `redact_fields` are replaced with `<redacted>`.

> **These logs are operator-only.** No MCP tool exposes them to Claude. Treat
> the directory like any production audit log: review before sharing, retain
> per your org's policy, and back up if you need historical analytics.

To enable verbose request tracing temporarily:

```bash
MCP_LOG_DEBUG_ENABLED=true MCP_LOG_LEVEL=DEBUG python -m src.server
```

All logs go to **stderr** (stdout is reserved for the MCP JSON-RPC protocol).

For the full debug/logging strategy and env-var reference, see
[`CLAUDE.md` § Debug & Logging Strategy](CLAUDE.md).

## Development Workflow

This project uses a **Context Engineering** workflow for non-trivial features.
The full description lives in
[`CLAUDE.md` § Context Engineering Workflow](CLAUDE.md). Quick summary:

```
1. Edit INITIAL.md          ← describe ONE feature (delta vs docs/plan.md)
2. /generate-prp INITIAL.md ← AI researches and writes PRPs/{feature}.md
3. /execute-prp PRPs/{...}  ← AI implements + runs ruff/mypy/pytest until green
```

`src/events/` is the project's reference implementation — new code mirrors its
patterns. See [`CLAUDE.md` § Reference Implementation](CLAUDE.md).

For small fixes (single-line changes, doc edits, etc.) skip the workflow and edit
directly.

## Development Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Project Setup | ✅ done |
| 2 | Core MCP Server | ✅ done — `list_apis`, registry, config loader, graceful shutdown |
| 3 | Authentication (OAuth + keyring) | ✅ done — PKCE flow, callback on `127.0.0.1`, `Credentials` with concurrent-refresh lock, 49 tests |
| 4 | API Gateway (REST + GraphQL) | ✅ done — `RestClient` + `GraphQLClient`, retry on 429/5xx + transport errors, redacted logging, response normalization, GraphQL partial-success preserved, 61 tests |
| 5 | Tools & Integration | ✅ done — `fetch_data`/`send_data`/`execute_graphql`/`get_status`, `auth.type` branching (oauth2/bearer/api_key/null), Recorder triple per call, secret redaction in insight events, 37 tests |
| 6 | Testing & Polish | ✅ done — subprocess smoke test, example-config schema-drift guard, README expanded with Quickstart / OAuth / Keyring per OS / Troubleshooting / Logging |
| 7 | Activity Logging (`src/events/`) | ✅ done — 27 test cases (51 collected with parametrization) |
| Post-v0 | GitHub OAuth integration | ✅ done — `scripts/oauth_login.py` operator CLI, real GitHub OAuth flow, verified on Claude Desktop **and** Codex CLI, 15 new tests |
| 8 | HTTP Transport | ✅ done — `MCP_TRANSPORT={stdio,http}` switch, Streamable HTTP via uvicorn + Starlette, Bearer-token middleware, loopback-bind safety guard, per-arg env fallback for `run_http` settings, single-tenant; 41 new tests (38 transport unit + 3 HTTP subprocess smoke) — 288 total |

Per-phase deliverables and verification plan: [`docs/plan.md`](docs/plan.md).
Future scalability ideas (web UI, multi-tenant, caching, additional transports,
public-deploy recipes) live in [`docs/plan.md` § Future Scalability](docs/plan.md).

## Security

- All credentials stored in OS-level secure keyring (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux)
- `.env` file excluded from version control via `.gitignore`
- OAuth uses standard authorization code flow with **PKCE** (no implicit grant)
- Tokens never logged or exposed in error messages — Pydantic `Field(repr=False)` keeps secret fields out of `repr()` and f-string output
- `OAuthConfig` rejects non-HTTPS `authorize_url` / `token_url` at validation time
- Local callback server binds to **`127.0.0.1`** (not `localhost`) and only during the OAuth flow; closes immediately after the auth code is received

## License

TBD

## Contributing

v0 (Phases 1–7) is shipped and the GitHub OAuth integration is wired up. The
active feature delta is **Phase 8 — HTTP transport** in [`INITIAL.md`](INITIAL.md);
implementation goes through `/generate-prp INITIAL.md` → `/execute-prp PRPs/{...}.md`
(see the [Development Workflow](#development-workflow) section). For bug fixes and
small edits, open a PR directly. Contribution guidelines (style, commit format,
review process) will be formalized as the project gains contributors.
