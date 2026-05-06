# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**MCP Data Gateway** — a Python-based Model Context Protocol (MCP) server that acts as a unified gateway to multiple external APIs. It provides Claude with tools to fetch and send data across REST and GraphQL endpoints, handling OAuth 2.0 authentication transparently.

The project is in **early development**. The plan is approved but no source code has been written yet — only `README.md` and this `CLAUDE.md`.

## Architecture

```
src/
├── server.py              # MCP server entry point (uses python `mcp` SDK)
├── auth/
│   ├── oauth.py           # OAuth 2.0 flow + automatic browser popup + local callback server
│   └── credentials.py     # Token storage via system keyring
├── gateway/
│   ├── api_client.py      # Generic async HTTP client (REST + GraphQL)
│   └── handlers.py        # Request/response normalization, GraphQL error parsing
├── models/
│   └── data_models.py     # Pydantic models for generic data shapes
└── tools/
    └── mcp_tools.py       # MCP tool definitions: fetch_data, send_data, execute_graphql, list_apis, get_status

config/
└── api_configs.json       # Per-API configuration (URL, auth method, endpoints)
```

### Key Design Decisions
- **Generic-first**: No hard-coded API integrations. All APIs configured via `config/api_configs.json`.
- **Auto-OAuth**: Tools detect missing/expired credentials and automatically trigger browser popup.
- **Async throughout**: Use `httpx` async client, `asyncio` for I/O.
- **Secure by default**: Credentials never written to disk in plaintext — always via `keyring`.

## Tech Stack

- **Python 3.10+**
- **mcp** — Model Context Protocol Python SDK
- **httpx** — Async HTTP (REST + GraphQL)
- **keyring** — Secure credential storage
- **pydantic** — Data validation
- **python-dotenv** — Environment configuration

## MCP Tools (planned)

| Tool | Purpose |
|------|---------|
| `fetch_data` | GET request to a configured API |
| `send_data` | POST/PUT request to a configured API |
| `execute_graphql` | Run GraphQL query/mutation |
| `list_apis` | List configured API services |
| `get_status` | Authentication and connection status |

All data-modifying tools auto-trigger OAuth popup when credentials are missing or expired.

## Development Conventions

### Code Style
- Type hints on all public functions and methods
- `async`/`await` for all I/O operations
- Pydantic models for any structured data crossing module boundaries
- Errors raised as specific exception classes, not bare `Exception`

### File Organization
- One module per logical concern (auth, gateway, tools, models)
- `__init__.py` files export the public API of each package
- Tests mirror source structure under `tests/`

### Configuration
- **Secrets** → `.env` (never committed)
- **API definitions** → `config/api_configs.json` (committed, but no secrets)
- **Defaults** → defined in code with overridable env vars

### Security Rules (Strict)
- Never log tokens, secrets, or full request/response bodies
- Never write credentials to plaintext files
- OAuth callback server binds only to `localhost`, only during the flow
- All `.env` and credential files listed in `.gitignore`

## Debug & Logging Strategy

### Log Levels
| Level | Use For |
|-------|---------|
| `DEBUG` | Full request/response details (URLs, headers, body — with secrets redacted) |
| `INFO` | Tool invocations, OAuth flow steps, successful API calls |
| `WARN` | Retries, deprecated config keys, soft failures |
| `ERROR` | Tool failures, OAuth failures, unrecoverable errors |

### Log Destination
- **Logs go to `stderr`**, never `stdout`. The MCP protocol uses `stdout` for JSON-RPC messages — writing logs there will corrupt the protocol stream.
- Default format: structured JSON (one event per line) for easy parsing.
- File logging optional via `MCP_LOG_FILE=/path/to/file.log`.

### Sensitive Data Redaction
Before logging any request/response, redact:
- `Authorization` header values
- `access_token`, `refresh_token`, `client_secret` fields in JSON bodies
- Query string parameters named `api_key`, `token`, `secret`, `password`

A central redaction helper lives in `src/gateway/handlers.py` — always route logs through it.

### Debug Mode
- `MCP_DEBUG=true` → enables verbose request tracing, dumps full (redacted) HTTP exchanges. **Default: `false`.**
- `MCP_LOG_LEVEL=DEBUG` → equivalent for log output level. **Default: `INFO`.**
- Both can be combined; debug mode also adds timing measurements per tool call

### Common Failure Modes
| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Tool hangs on first call | OAuth callback port already in use | Set `OAUTH_CALLBACK_PORT` to a free port |
| `keyring.errors.NoKeyringError` | Headless Linux without DBus | Install `keyrings.alt` or use file-based fallback |
| 401 after working previously | Token expired, refresh token invalid | Re-run OAuth flow (delete keyring entry) |
| GraphQL "success" but empty data | Errors in response body, not HTTP status | Check `response.errors[]` in GraphQL handler |
| Tool returns truncated data | Response exceeded size limit | Use pagination params or increase `MCP_MAX_RESPONSE_BYTES` |

## Response Format Conventions

All MCP tools return structured JSON, never bare strings. This makes responses machine-parseable for Claude and gives a consistent shape across REST and GraphQL backends.

### Success Shape
```json
{
  "data": <api response payload>,
  "metadata": {
    "source": "example_api",
    "endpoint": "get_users",
    "timestamp": "2026-05-06T10:30:00Z",
    "duration_ms": 142
  }
}
```

### Error Shape
```json
{
  "error": {
    "code": "AUTH_REQUIRED",
    "message": "Authentication needed for example_api. Browser popup opened.",
    "details": { "provider": "google", "scopes": ["read"] }
  }
}
```

### Standard Error Codes
| Code | Meaning |
|------|---------|
| `AUTH_REQUIRED` | OAuth flow needed — popup triggered |
| `AUTH_FAILED` | OAuth completed but token rejected by API |
| `API_NOT_CONFIGURED` | Requested API not in `api_configs.json` |
| `ENDPOINT_NOT_FOUND` | API exists but endpoint key is undefined |
| `RATE_LIMITED` | API returned 429 — includes `retry_after` in details |
| `UPSTREAM_ERROR` | API returned 5xx |
| `VALIDATION_ERROR` | Request body failed Pydantic validation |
| `RESPONSE_TOO_LARGE` | Response exceeded cap **and could not be safely truncated** (binary/streaming) |

### Large Responses — Truncation Rule
The default behavior for oversized responses is **success with truncation**, not error. The `RESPONSE_TOO_LARGE` error code is only emitted when truncation isn't safe.

| Payload type | Behavior when over `MCP_MAX_RESPONSE_BYTES` |
|--------------|---------------------------------------------|
| JSON / text | Truncate at byte boundary, return success + `metadata.truncated: true` |
| Paginated list | Truncate, surface pagination cursor in `metadata.next_cursor` |
| Binary (base64) | **Error** with `RESPONSE_TOO_LARGE` — partial binary is unusable |
| Streaming / chunked | **Error** with `RESPONSE_TOO_LARGE` — partial stream is unusable |

**Truncation success shape:**
```json
{
  "data": <truncated payload>,
  "metadata": {
    "truncated": true,
    "total_bytes": 5242880,
    "returned_bytes": 1048576,
    "next_cursor": "...",
    "hint": "Use pagination params or filter to narrow results"
  }
}
```

For paginated APIs, always surface pagination tokens/cursors in `metadata` so Claude can request more.

### Binary Data
- Base64-encode bytes; never raw binary in JSON.
- Always include `metadata.content_type` (e.g., `image/png`) and `metadata.encoding: "base64"`.

### GraphQL Specifics
- GraphQL can return *both* data and errors. Surface both:
  ```json
  {
    "data": { "user": { "name": "Ada" } },
    "errors": [
      { "message": "Field 'avatar' unauthorized", "path": ["user", "avatar"] }
    ],
    "metadata": { ... }
  }
  ```
- Do NOT collapse partial-success into a flat error — Claude can use the partial data.

## Common Commands (once implemented)

```bash
# Install dependencies
pip install -r requirements.txt

# Run the MCP server
python -m src.server

# Run tests
pytest tests/

# Run linter
ruff check src/

# Format code
ruff format src/
```

## Implementation Phases (Reference)

The committed plan is at [`docs/plan.md`](docs/plan.md). Six phases:

1. **Project Setup** — structure, dependencies, `.gitignore`
2. **Core MCP Server** — server bootstrap, tool registration
3. **Authentication** — OAuth flow, keyring storage, token refresh
4. **API Gateway** — generic REST + GraphQL client
5. **Tools & Integration** — implement each MCP tool
6. **Testing & Documentation** — unit/integration tests, examples

## When Adding a New API

1. Add an entry to `config/api_configs.json` with `base_url`, `type` (`rest`/`graphql`), `auth` block, and `endpoints`.
2. No code changes should be required for standard REST/GraphQL APIs.
3. Custom auth providers may need additions to `src/auth/oauth.py`.

## Things to Watch For

- **Token refresh races**: When multiple tools fire concurrently with an expired token, ensure only one refresh happens.
- **Callback port conflicts**: The OAuth callback port (default 8765) must be free; configurable via `OAUTH_CALLBACK_PORT`.
- **Error normalization**: REST errors come from HTTP status; GraphQL errors come in the response body even when HTTP is 200.
- **Keyring availability**: Some headless Linux environments lack a keyring backend — provide a clear error message if so.

## Out of Scope (For Now)

- Database / persistent storage for fetched data
- Multi-user / multi-tenant credential separation
- Webhook receivers
- Web UI (planned for the future "MCP App" evolution)
- Rate limiting and caching layers
