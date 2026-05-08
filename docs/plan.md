# MCP Data Gateway — Implementation Plan

> **Workflow.** This file is the high-level roadmap (what to build, in what order). For
> the implementation workflow itself — Context Engineering with `/generate-prp` and
> `/execute-prp` — see [`CLAUDE.md` § Context Engineering Workflow](../CLAUDE.md). New
> features start by writing a delta in [`INITIAL.md`](../INITIAL.md).
>
> **Status (2026-05-08):** Phases 1–8 shipped + GitHub OAuth integration shipped.
> **288 tests passing.** Verified end-to-end against Claude Desktop and OpenAI Codex
> CLI over stdio, and against the Streamable HTTP test suite (38 transport unit tests
> + 3 subprocess smoke tests
> covering Bearer auth, loopback guard, and a full initialize → tools/list round-trip).
> Selectable via `MCP_TRANSPORT={stdio,http}`; stdio remains the default.

## Context
Building a Python-based **Model Context Protocol (MCP) server** that acts as a data gateway, enabling Claude to send/receive data from various external APIs via a unified interface. The system features OAuth 2.0 authentication and supports generic REST/GraphQL API integration, with a foundation for future evolution into a standalone MCP App.

**User Requirements:**
- Handle multiple generic data types
- Connect to external APIs through a generic API gateway approach
- OAuth 2.0 authentication system
- Python-based implementation
- Scalable foundation for future MCP App expansion

## Architecture Overview

### 1. Core MCP Server (`src/server.py`)
- Initialize MCP server using the `mcp` Python library
- Define and register MCP tools that Claude can invoke
- Handle tool execution and error responses
- Manage lifecycle (startup, shutdown, resource cleanup)

### 2. Authentication Module (`src/auth/`)
- **oauth.py**: OAuth 2.0 + PKCE flow handler
  - Support multiple OAuth providers (Google, GitHub, custom endpoints)
  - Browser-based authorization code flow with `127.0.0.1` callback server
  - Driven by the `scripts/oauth_login.py` operator CLI (one-time per provider);
    MCP tools surface `AUTH_REQUIRED` rather than auto-opening the browser
  - Token generation, refresh, and storage
  - Authorization code exchange
- **credentials.py**: Secure credential management
  - Store/retrieve tokens from local encrypted storage (using `keyring`)
  - Credential validation and expiration checks
  - Auto-prompt for re-authentication when token expires

### 3. API Gateway (`src/gateway/`)
- **api_client.py**: Generic HTTP client (REST + GraphQL) built on `httpx`
  - REST: Standard GET/POST/PUT/DELETE with query/body parameters
  - GraphQL: Query/mutation execution with variable support
  - Multiple authentication methods (Bearer tokens, API keys, Basic auth)
  - Request/response serialization
  - Error handling and retry logic
- **handlers.py**: Request/response processing
  - Data transformation and validation
  - Error normalization across different API responses
  - GraphQL error handling (parsing GraphQL errors separately from HTTP errors)
  - Sensitive-data redaction helper used by logging

### 4. Data Models (`src/models/`)
- **data_models.py**: Generic Pydantic data structures
  - Base model for flexible data handling
  - Support arbitrary JSON structures
  - Metadata fields (source API, timestamp, etc.)

### 5. MCP Tools (`src/tools/`)
- **mcp_tools.py**: Define MCP tools that Claude can use
  - `send_data`: POST/PUT data to external APIs (returns `AUTH_REQUIRED` if no token)
  - `fetch_data`: GET data from external APIs with filtering (returns `AUTH_REQUIRED` if no token)
  - `execute_graphql`: Execute GraphQL queries/mutations (returns `AUTH_REQUIRED` if no token)
  - `list_apis`: Show available API configurations
  - `get_status`: Check authentication and API connection status
  - Auth resolution: tools check for valid credentials, refresh silently when ≤ 5 min from expiry, otherwise return `AUTH_REQUIRED` so the operator can run `scripts/oauth_login.py`

### 6. Configuration System
- `.env` / `.env.example`: Environment variables for credentials, OAuth, logging, response limits
- `config/api_configs.json`: Define available APIs with:
  - API endpoint URLs
  - Authentication type (OAuth, API key, Bearer token)
  - Supported operations (GET, POST, etc.)
  - Data mapping schemas
  - Rate limits and timeouts

## Implementation Strategy

### Phase 1: Project Setup ✓ (complete)
1. Initialize Python project structure
2. Create `requirements.txt` with dependencies:
   - `mcp` (Model Context Protocol SDK)
   - `httpx` (async HTTP client)
   - `keyring` (secure credential storage)
   - `python-dotenv` (environment variables)
   - `pydantic` (data validation)
   - Note: `webbrowser` is in Python's standard library — do not list as a pip dependency
3. Create `requirements-dev.txt` for `pytest` and `pytest-asyncio`
4. Create `pyproject.toml` for pytest configuration (`asyncio_mode = "auto"`)
5. Set up `.gitignore` for sensitive files (`.env`, credentials, caches, `logs/`)
6. Create `.env.example` documenting all environment variables
7. Basic `README.md` with architecture overview
8. _Remaining_: initial `config/api_configs.json` template

### Phase 2: Core MCP Server ✓ (complete)
1. ✓ MCP server initialization (`src/server.py`)
2. ✓ Tool registry (`ToolSpec`, `ToolRegistry`) with `list_apis` built-in tool
3. ✓ Config loader with `${VAR}` substitution (`src/config.py`)
4. ✓ Logging to stderr only; stdout reserved for the MCP protocol
5. ✓ Async request pipeline + graceful SIGINT/SIGTERM shutdown

### Phase 3: Authentication ✓ (complete)
1. ✓ OAuth 2.0 authorization code flow with PKCE (`src/auth/oauth.py`)
2. ✓ Local callback HTTP server bound to `127.0.0.1` (NOT `localhost`), only during the flow
3. ✓ HTTPS-only validator on `authorize_url` / `token_url`
4. ✓ Credential storage via `keyring`, JSON-serialized `TokenInfo` (`src/auth/credentials.py`)
5. ✓ Token refresh with `asyncio.Lock` per `api_id` to prevent concurrent-refresh races
6. ✓ Read-only `peek()` API for status checks that must not trigger OAuth
7. ✓ `Field(repr=False)` on secret fields keeps tokens out of `repr()` / log output

### Phase 4: API Gateway ✓ (complete)
1. ✓ Generic async HTTP client (`src/gateway/api_client.py`) — `RestClient` (GET/POST/PUT/DELETE/PATCH) + `GraphQLClient` ({query, variables, operationName})
2. ✓ Header precedence: per-request > optional `auth_provider` > defaults
3. ✓ Retry on HTTP 429 / 502 / 503 / 504 (NOT 500); honors `Retry-After`; exponential backoff capped at 8s
4. ✓ Retry on transport errors (`ConnectError`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout`, `RemoteProtocolError`)
5. ✓ Response normalization (`src/gateway/handlers.py`) — HTTP status → standard error codes; rate-limit headers → metadata
6. ✓ GraphQL partial-success preserved: `{data, errors, metadata}` not collapsed to flat error
7. ✓ Size enforcement via `MCP_MAX_RESPONSE_BYTES`: JSON/text truncates with metadata; binary → `RESPONSE_TOO_LARGE`
8. ✓ All HTTP traffic logging routes through `src/events/redaction.py` (no Bearer tokens in logs)

### Phase 5: Tools & Integration ✓ (complete)
1. ✓ Remaining MCP tools shipped — `fetch_data`, `send_data`, `execute_graphql`, `get_status` (`list_apis` was Phase 2)
2. ✓ Standard response shape from CLAUDE.md observed everywhere; error responses are `{error: ...}` only with no top-level `metadata` sibling
3. ✓ Large-response handling delegated to Phase 4's `normalize_*_response` (truncate + metadata; binary/streaming → `RESPONSE_TOO_LARGE`)
4. ✓ Auth header resolution via `src/tools/auth_resolver.py` — branches on `auth.type` ∈ {`oauth2`, `bearer`, `api_key`, `None`}; `KNOWN_AUTH_TYPES` constant prevents drift between `resolve_auth_headers` (request path) and `peek_auth_state` (read-only `get_status`). Tools never auto-trigger an OAuth browser flow — they raise `AuthRequiredError`, which `mcp_tools.py` surfaces as the `AUTH_REQUIRED` error response.
5. ✓ `Recorder` triple in `try/finally` per tool — `record_audit` + `record_usage` + `record_insight` fire on every code path; `tool_args` runs through `redact_body` before insight emission
6. ✓ `get_status` uses `Credentials.peek()` exclusively — never refreshes, never opens a browser; tested with `OAuth.start_flow` spy
7. ✓ `ApiAuthConfig.client_secret` field added to support OAuth refresh; `_build_oauth_configs` skips with warning when required fields are missing instead of silently constructing a broken config

### Phase 6: Testing & Documentation ✓ (complete)
1. ✓ Unit tests per module — 220 unit tests across `tests/auth/` (49), `tests/events/` (51 collected), `tests/gateway/` (61), `tests/tools/` (37), and the top-level `tests/test_config.py` + `tests/test_server.py` (22 combined), plus 5 integration tests and 7 example-config drift tests for **232 total**
2. ✓ Integration tests with mock APIs — `tests/integration/test_full_flow.py` (real Recorder + RestClient + Credentials with pre-stored token, secret-omission canary on captured JSONL) + `tests/integration/test_smoke.py` (subprocess server boot/SIGTERM/exit, two secret-leak canaries on stderr and JSONL)
3. ✓ Refined `config/api_configs.example.json` — three auth-type examples (oauth2, bearer, api_key) + a no-auth example; expanded top-level `_comment` documenting the four `auth.type` paths and `redact_fields` semantics
4. ✓ Schema-drift guard — `tests/test_example_config.py` validates the example file against `ApiConfigsRoot`, enforces `${VAR}` placeholders for credentials, asserts `localhost` is not used in OAuth callbacks, and refuses literal token prefixes (ghp_, sk-, xoxb-, AKIA, …)
5. ✓ README expanded — Quickstart (5-step verbatim), Configuring an API, OAuth Setup, Keyring per OS table, Troubleshooting (9 symptom→cause→fix rows), Logging (operator-only)
6. ✓ `MCP_API_CONFIG_PATH` env var added — operator override for the API config file path; also enables test isolation

### Phase 7: Activity Logging (`src/events/`) ✓ (complete)
1. ✓ Pydantic schemas for four event categories: `audit`, `debug`, `usage`, `insight`
2. ✓ Centralized redaction helper (headers, body keys, URL query params)
3. ✓ Async JSONL writer with buffered queue and per-month file rotation
4. ✓ Retention cleanup (delete files older than `MCP_LOG_RETENTION_DAYS`, never the current month)
5. ✓ Public `Recorder` API integrated by tools, gateway, and auth modules
6. ✓ Per-API payload depth controls in `config/api_configs.json` (`metadata`/`summary`/`full`)
7. ✓ Logs are operator-only — **not** exposed via any MCP tool

### Post-v0 — GitHub OAuth integration ✓ (complete, committed `82f8165`)
1. ✓ `scripts/oauth_login.py` — operator CLI to drive OAuth 2.0 + PKCE flow and persist
   the token in keyring (works around the design choice that MCP tools surface
   `AUTH_REQUIRED` rather than auto-opening the browser themselves)
2. ✓ `config/api_configs.json` — first real API entry (GitHub) with `read:user` /
   `public_repo` scopes and `email` / `notification_email` PII redaction
3. ✓ `src/server.py` loads `.env` at startup so `${VAR}` placeholders resolve when
   spawned by Claude Desktop or Codex CLI from a foreign cwd
4. ✓ Verified end-to-end on Claude Desktop **and** OpenAI Codex CLI (both stdio)
5. ✓ `.env.example` documents the per-provider credential pattern
6. ✓ 15 new tests (5 helper unit + 10 CLI integration); test count: 232 → **247**

### Phase 8: HTTP Transport ✓ (complete)
1. ✓ `MCP_TRANSPORT={stdio,http}` switch in `src/server.py`; stdio remains default
2. ✓ `src/transport/` package — `transport/stdio.py` (extracted) + `transport/http.py`
   (Streamable HTTP via `mcp.server.streamable_http_manager.StreamableHTTPSessionManager`,
   wrapped in a Starlette app with lifespan + Mount + uvicorn `Server.serve()`)
3. ✓ Pure-ASGI Bearer-token middleware (NOT `BaseHTTPMiddleware` — would buffer
   SSE responses); `secrets.compare_digest` on equal-length bytes
4. ✓ `LoopbackGuardError` fail-loud refuse-to-start when bound non-loopback
   without a token configured
5. ✓ New env vars: `MCP_TRANSPORT`, `MCP_HTTP_HOST` (default `127.0.0.1`),
   `MCP_HTTP_PORT` (default `8080`), `MCP_HTTP_BEARER_TOKEN`
6. ✓ `uvicorn` + `starlette` promoted from transitive (`mcp`) to direct deps
7. ✓ 38 tests under `tests/transport/` + 3 subprocess smoke tests at
   `tests/integration/test_http_smoke.py` — dispatcher routing, loopback
   guard parametrized over loopback/public hosts, Bearer auth
   (missing/wrong/length-mismatch/correct), 401 body never echoes the
   supplied token, `resolve_http_settings` env-var validation, `run_http`
   per-arg env fallback (explicit / no-args / partial overlay /
   explicit-empty-token), `/mcp` no-redirect regression, full
   initialize → notifications/initialized → tools/list round-trip via
   Starlette `TestClient`, and a real-uvicorn boot + 401/200/404 +
   loopback-guard-exit + JSONL-leak canary at the OS-process level
8. ✓ Shared subprocess scaffolding extracted to
   `tests/integration/_helpers.py` (`spawn_server`, `isolated_env`,
   `wait_for_ready`) — used by both stdio and HTTP smoke tests
9. Out of scope (deferred): WebSocket, legacy SSE, multi-tenant token
   isolation, public-deploy recipes (Dockerfile, systemd, reverse proxy)

## Critical Files (status)
- `src/server.py` — MCP server entry point ✓ **implemented**
- `src/config.py` — API config loader with `${VAR}` substitution; `ApiAuthConfig` includes `client_secret` ✓ **implemented**
- `src/tools/spec.py`, `src/tools/registry.py`, `src/tools/builtin.py` — tool registry + `list_apis` ✓ **implemented**
- `src/auth/oauth.py` — OAuth 2.0 + PKCE flow ✓ **implemented**
- `src/auth/credentials.py` — Keyring-backed credential store with concurrent-refresh lock + `peek()` ✓ **implemented**
- `src/events/` — Activity logging (schemas, writers, retention, recorder) ✓ **implemented**
- `src/gateway/api_client.py` — Generic REST/GraphQL client with retry + transport-error retry ✓ **implemented**
- `src/gateway/handlers.py` — Response normalization (reuses `src/events/redaction.py`) ✓ **implemented**
- `src/tools/context.py` — `ToolContext` dependency container ✓ **implemented**
- `src/tools/auth_resolver.py` — `resolve_auth_headers` + `peek_auth_state` (auth.type branching) ✓ **implemented**
- `src/tools/mcp_tools.py` — `fetch_data`/`send_data`/`execute_graphql`/`get_status` + Recorder triple ✓ **implemented**
- `src/models/data_models.py` — Pydantic models for responses (out of scope; tools rely on dict shapes from CLAUDE.md)
- `config/api_configs.json` — API configuration template (gitignored runtime file; example committed at `config/api_configs.example.json`)
- `tests/events/` — 27 passing unit tests for `src/events/` ✓ **implemented**
- `tests/auth/` — 49 passing unit tests for `src/auth/` ✓ **implemented**
- `tests/gateway/` — 61 passing unit tests for `src/gateway/` ✓ **implemented**
- `tests/tools/` — 37 passing unit tests for `src/tools/` ✓ **implemented**
- `tests/test_config.py`, `tests/test_server.py` — config + server bootstrap + `_build_oauth_configs` tests ✓ **implemented**
- `tests/test_example_config.py` — 7 schema-drift / placeholder-enforcement tests for `config/api_configs.example.json` ✓ **implemented**
- `tests/integration/` — full-flow + subprocess smoke tests (5 cases total) ✓ **implemented**

## Technology Stack

See [`README.md` § Tech Stack](../README.md). Source of truth for runtime versions:
[`requirements.txt`](../requirements.txt).

## Verification Plan
1. **Unit tests** per module (auth, gateway, handlers, tools)
2. **Integration tests** with mock external APIs
3. **Manual testing**:
   - Start MCP server (`python -m src.server`)
   - Configure a sample API in `config/api_configs.json`
   - Invoke `fetch_data` from Claude → verify it returns `AUTH_REQUIRED` cleanly
   - Run `python -m scripts.oauth_login <api_id>` → browser flow + token in keyring
   - Re-invoke `fetch_data` → second call succeeds without re-auth
   - Verify large responses truncate with metadata
   - Verify GraphQL partial-success responses surface both data and errors
4. **Security validation**:
   - Verify credentials never appear in logs (DEBUG and INFO levels)
   - Confirm tokens encrypted in keyring
   - Confirm callback server only listens on `127.0.0.1` (not `localhost`) and only during OAuth

## Future Scalability
- **MCP App evolution** — extract this into a backend behind a web frontend
- **Persistent storage** — SQLite/Postgres for data history and audit logs
- **Advanced features** — rate limiting, response caching, transformation pipelines
- **Multi-tenant** — separate credential stores per user (not addressed by Phase 8;
  Phase 8 ships single-tenant HTTP only)
- **Additional transports** — WebSocket (real-time bidirectional), legacy SSE
  (only if needed for backward compat). Both are deferred until concrete client
  demand exists.
- **Public-deploy recipes** — Dockerfile, systemd / launchd unit files,
  reverse-proxy / TLS setup. Doc-only follow-up after Phase 8.
