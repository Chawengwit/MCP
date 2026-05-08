# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**MCP Data Gateway** — a Python-based Model Context Protocol (MCP) server that acts as a unified gateway to multiple external APIs. It provides Claude with tools to fetch and send data across REST and GraphQL endpoints, handling OAuth 2.0 authentication transparently.

Phases 1–8 are implemented and tested (**288 passing unit + integration tests**):

- **Phase 1** — project setup
- **Phase 2** — core MCP server (`src/server.py`), config loader (`src/config.py`), tool registry (`src/tools/`)
- **Phase 3** — authentication subsystem (`src/auth/`): OAuth 2.0 + PKCE + keyring credentials
- **Phase 4** — API gateway (`src/gateway/`): `RestClient` + `GraphQLClient` + response normalization
- **Phase 5** — full MCP tool surface (`src/tools/`: `list_apis`, `fetch_data`, `send_data`, `execute_graphql`, `get_status`)
- **Phase 6** — integration tests (subprocess smoke test + full-flow), example-config schema-drift guard, README expanded with Quickstart / OAuth / Keyring per OS / Troubleshooting / Logging
- **Phase 7** — activity logging subsystem (`src/events/`)
- **Post-v0** — GitHub OAuth integration shipped (`scripts/oauth_login.py`, `config/api_configs.json`); verified end-to-end on Claude Desktop and OpenAI Codex CLI (both stdio).
- **Phase 8** — HTTP transport (`src/transport/`): Streamable HTTP via uvicorn + Starlette, Bearer-token middleware, loopback-bind-without-token guard. Selectable via `MCP_TRANSPORT={stdio,http}` env var; stdio remains the default.

Future work (multi-tenant, public-deploy recipes, additional transports) lives in [`docs/plan.md` § Future Scalability](docs/plan.md).

## Architecture

For the full directory tree, module responsibilities, and tech stack, see
[`README.md` § Architecture](README.md). For phase-by-phase implementation breakdown,
see [`docs/plan.md`](docs/plan.md).

What this file pins:

- **`src/events/`** is the project's reference implementation (Phase 7, complete). All
  new code mirrors its conventions. See "Reference Implementation" section below.
- **`src/server.py`, `src/config.py` (Phase 2)**, **`src/auth/` (Phase 3)**,
  **`src/gateway/` (Phase 4)**, **`src/tools/` (Phase 5 — `fetch_data`/`send_data`/
  `execute_graphql`/`get_status` plus the existing `list_apis`)**, and **the integration
  tests + docs polish (Phase 6)** are also complete and follow the `src/events/` patterns.
- **`scripts/oauth_login.py`** — operator CLI that drives the OAuth 2.0 + PKCE flow and
  persists the resulting token in keyring. MCP tools intentionally surface
  `AUTH_REQUIRED` rather than auto-opening a browser, so this script is the
  user-facing counterpart for first-time login per provider.
- **`src/transport/`** (Phase 8) — transport-layer abstraction. `src/server.py`
  builds a single `Server` instance and dispatches to `run_stdio` or `run_http`
  based on `MCP_TRANSPORT`. Tool handlers are transport-agnostic; only the wire
  format around them changes. New transports plug in here without touching
  tools / gateway / auth.
- New features start with a delta in [`INITIAL.md`](INITIAL.md), then
  `/generate-prp` → `/execute-prp`.

### Transport Selection

| `MCP_TRANSPORT` | Module | Used by |
|-----------------|--------|---------|
| `stdio` (default) | `src/transport/stdio.py` | Claude Desktop, Codex CLI, any process-spawn MCP client |
| `http` | `src/transport/http.py` | ChatGPT Connectors, MCP Inspector, remote HTTP clients |

**HTTP-mode invariants** (enforced in code):
- Loopback binds (`127.0.0.1`, `::1`, `localhost`) are allowed without `MCP_HTTP_BEARER_TOKEN`.
- Any other host **without** a bearer token → fail-loud `LoopbackGuardError` at startup.
- Bearer comparison uses `secrets.compare_digest` on equal-length bytes; length mismatch short-circuits to `401`.
- Bearer middleware is a **pure-ASGI** callable (NOT `BaseHTTPMiddleware`) — `BaseHTTPMiddleware` buffers responses and would break the SSE streams Streamable HTTP can return.
- `uvicorn` owns SIGINT/SIGTERM; the stdio branch keeps the existing `loop.add_signal_handler` setup. Don't double-install.
- All logs stay on **stderr** in both transports (uvicorn `log_config=None`, `access_log=False`). The stdout reservation isn't strictly needed in HTTP mode, but keeping the discipline avoids future transport-toggle surprises.

### Key Design Decisions
- **Generic-first**: No hard-coded API integrations. All APIs configured via `config/api_configs.json`.
- **Operator-driven OAuth**: Tools detect missing/expired credentials and return `AUTH_REQUIRED` so the client surfaces it cleanly. The browser flow runs out-of-band via `python -m scripts.oauth_login <api_id>`. Once the token is stored in keyring, subsequent calls auto-refresh near expiry inside `Credentials.get()`.
- **Async throughout**: Use `httpx` async client, `asyncio` for I/O.
- **Secure by default**: Credentials never written to disk in plaintext — always via `keyring`.

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
- OAuth callback server binds only to `127.0.0.1` (NOT `localhost` — some browsers treat them as different origins for OAuth state tracking), only during the flow
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

The central redaction helpers live in [`src/events/redaction.py`](src/events/redaction.py) (`redact_headers`, `redact_body`, `redact_url`) — always route logs through them. The future `src/gateway/handlers.py` should reuse these helpers, not reimplement them.

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

## Activity Logging (`src/events/`)

The gateway records every tool invocation across **four separate categories** to JSONL files. These logs are operator-only — **no MCP tool exposes them to Claude**.

### Categories

| Category | Path | Purpose |
|----------|------|---------|
| `audit` | `logs/audit/YYYY-MM.jsonl` | Who/when/what — security & compliance |
| `debug` | `logs/debug/YYYY-MM.jsonl` | Full HTTP exchange (redacted) for troubleshooting |
| `usage` | `logs/usage/YYYY-MM.jsonl` | Per-call metrics (latency, sizes) — analytics |
| `insight` | `logs/insight/YYYY-MM.jsonl` | Tool args + response summaries — Claude's request patterns |

### File Layout
- One JSONL file per **category per month** (`YYYY-MM.jsonl`).
- Append-only; one event per line.
- File created lazily on first event of the month.
- Retention: 1 year (configurable via `MCP_LOG_RETENTION_DAYS`). Cleanup runs when a new monthly file is created. The current month is never deleted.

### Public API
```python
from src.events import Recorder, ResponseSummary

recorder = Recorder.from_env()
await recorder.start()  # at server startup

# In each tool implementation:
await recorder.record_audit(session_id=..., tool="fetch_data", result="success", duration_ms=42)
await recorder.record_usage(tool="fetch_data", status="success", duration_ms=42)
await recorder.record_insight(session_id=..., tool="fetch_data", tool_args={...})
# In gateway/api_client.py:
await recorder.record_debug(session_id=..., tool="fetch_data", request=..., response=..., duration_ms=42)

await recorder.stop()  # at shutdown — drains queue, closes handles
```

### Recording Rules
- All `record_*` methods are **async non-blocking** (enqueue + background writer task).
- All recording is wrapped in try/except — **never raises** to the tool path. Failures emit a stderr warning.
- Tools should call `record_audit` + `record_usage` + `record_insight` for every invocation.
- The gateway calls `record_debug` only when `MCP_LOG_DEBUG_ENABLED=true`.

### Per-API Payload Depth
`config/api_configs.json` controls how much of each request/response is captured for the **debug** category:

```json
{
  "apis": {
    "example_api": {
      "logging": {
        "request_payload": "metadata",
        "response_payload": "summary",
        "redact_fields": ["ssn", "credit_card"]
      }
    }
  }
}
```

Modes: `metadata` (headers + size only), `summary` (metadata + first/last 200 bytes + top keys), `full` (after redaction). Default: `metadata`.

### Redaction
The redaction helpers in `src/events/redaction.py` always strip:
- Headers: `Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `Proxy-Authorization`
- Body keys: `password`, `token`, `access_token`, `refresh_token`, `client_secret`, `api_key`, `secret`, plus per-API `redact_fields`
- URL query params: `api_key`, `token`, `secret`, `password`

Replacement: the literal string `<redacted>` (preserves the field name and JSON shape).

### Things to Watch For
- **UTC everywhere**: timestamps and filename months use UTC to avoid month-boundary off-by-ones.
- **Writer is single-task**: don't instantiate multiple `JsonlWriter` instances pointing at the same `log_dir` in one process — file corruption.
- **Queue overflow drops the new event**: when `queue_max_size` is exceeded, `submit()` catches `QueueFull` and drops the *incoming* event (with stderr warning). The queue is FIFO and existing events are preserved. Default cap is 10,000 — generous.
- **Cleanup runs in a thread**: `cleanup_old_logs` uses synchronous `pathlib`, scheduled via `asyncio.to_thread` to keep the event loop responsive.
- **`Recorder.from_env()` reads env at construction** — changes to `MCP_LOG_*` after start are not picked up.

## Context Engineering Workflow

This project uses a **Context Engineering** workflow for non-trivial features. The two-step
loop is:

```
1. Edit INITIAL.md          ← describe ONE feature (FEATURE / EXAMPLES / DOCS / CONSIDERATIONS)
2. /generate-prp INITIAL.md ← AI researches and produces PRPs/{feature}.md
3. /execute-prp PRPs/{...}  ← AI implements + runs validation gates until green
```

### Files & Locations

| Path | Purpose |
|------|---------|
| [`INITIAL.md`](INITIAL.md) | Feature request — overwritten or copied per feature |
| [`.claude/commands/generate-prp.md`](.claude/commands/generate-prp.md) | Slash command that produces a PRP from `INITIAL.md` |
| [`.claude/commands/execute-prp.md`](.claude/commands/execute-prp.md) | Slash command that implements a PRP |
| [`PRPs/templates/prp_base.md`](PRPs/templates/prp_base.md) | Template the agent fills in |
| `PRPs/{feature}.md` | Generated blueprint per feature (committed) |
| [`config/api_configs.example.json`](config/api_configs.example.json) | Per-API config template |

> Reference code patterns live in `src/events/` directly — no separate `examples/` folder.
> See "Reference Implementation" below.

### Validation Gates (run during `/execute-prp`)

```bash
ruff check src/ scripts/ tests/ --fix
ruff format src/ scripts/ tests/
mypy src/ scripts/ tests/
pytest tests/ -v          # baseline 288 passing; new features add to that count
```

### When to Use

- ✅ New module or phase (Phase 2 server, Phase 3 auth, Phase 4 gateway, Phase 5 tools)
- ✅ Cross-cutting changes (e.g. adding a new error code across all tools)
- ❌ Small bug fixes or single-line changes — just edit directly
- ❌ Exploratory spikes where requirements aren't yet clear

### Reference Implementation

`src/events/` is the gold standard. Every PRP should cite it as the pattern source for:
- Async + queue-based design ([writers.py](src/events/writers.py))
- Pydantic v2 schemas ([schemas.py](src/events/schemas.py))
- Public-class API with `from_env()` ([recorder.py](src/events/recorder.py))
- Sensitive-data redaction ([redaction.py](src/events/redaction.py)) — **always reuse**
- Test layout with pytest-asyncio auto mode ([tests/events/](tests/events/))

## Helper Scripts (`scripts/`)

CLI utilities outside the MCP server's runtime path. They share the same
project-root bootstrap and `.env` loading as `src/server.py`, so any new
script should follow that pattern.

| Script | Purpose |
|--------|---------|
| `scripts/oauth_login.py` | Run the OAuth 2.0 + PKCE flow for an API in `api_configs.json`, store the token in keyring. Use when `fetch_data` / `send_data` returns `AUTH_REQUIRED`. |

```bash
python -m scripts.oauth_login github           # log in
python -m scripts.oauth_login github --clear   # delete + re-log in
```

The MCP tools intentionally do NOT trigger the browser flow themselves —
they return `AUTH_REQUIRED` so Claude can surface it cleanly. Operators
run `scripts/oauth_login.py` once per provider; the stored token is then
auto-refreshed by `Credentials.get()` near expiry.

## When Adding a New API

1. Add an entry to `config/api_configs.json` with `base_url`, `type` (`rest`/`graphql`), `auth` block, and `endpoints`.
2. No code changes should be required for standard REST/GraphQL APIs.
3. Custom auth providers may need additions to `src/auth/oauth.py`.
4. For OAuth APIs: run `python -m scripts.oauth_login <api_id>` to perform the initial login.

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
