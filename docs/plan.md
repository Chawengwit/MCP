# MCP Data Gateway — Implementation Plan

> **Workflow.** This file is the high-level roadmap (what to build, in what order). For
> the implementation workflow itself — Context Engineering with `/generate-prp` and
> `/execute-prp` — see [`CLAUDE.md` § Context Engineering Workflow](../CLAUDE.md). New
> features for Phases 2–6 start by writing a delta in [`INITIAL.md`](../INITIAL.md).

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
- **oauth.py**: OAuth 2.0 flow handler with automatic popup
  - Support multiple OAuth providers (Google, GitHub, custom endpoints)
  - Automatic browser popup on first tool invocation requiring auth
  - Local HTTP server to handle OAuth callback
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
  - `send_data`: POST/PUT data to external APIs (triggers OAuth popup if needed)
  - `fetch_data`: GET data from external APIs with filtering (triggers OAuth popup if needed)
  - `execute_graphql`: Execute GraphQL queries/mutations (triggers OAuth popup if needed)
  - `list_apis`: Show available API configurations
  - `get_status`: Check authentication and API connection status
  - Auto-authentication: tools check for valid credentials, trigger OAuth popup if missing/expired

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

### Phase 4: API Gateway
1. Build generic HTTP client supporting multiple auth methods
2. Implement request/response handlers with error normalization
3. Add request validation against API configs
4. Implement central redaction helper for sensitive data in logs

### Phase 5: Tools & Integration
1. Implement remaining MCP tools (`fetch_data`, `send_data`, `execute_graphql`, `get_status`); `list_apis` already shipped in Phase 2
2. Standardize response shape (success: `data` + `metadata`; error: `error` with code/message/details)
3. Implement large-response handling (truncate + metadata; only return `RESPONSE_TOO_LARGE` when truncation isn't safe — e.g., binary/streaming)
4. Integrate auto-authentication with tool execution
5. **Wire `Recorder` into the tool call path** — every invocation calls `record_audit` + `record_usage` + `record_insight`; gateway calls `record_debug` only when `MCP_LOG_DEBUG_ENABLED=true`. See [`CLAUDE.md` § Activity Logging > Recording Rules](../CLAUDE.md).

### Phase 6: Testing & Documentation
1. Unit tests per module
2. Integration tests with mock APIs
3. Sample `config/api_configs.json` examples
4. Setup/usage documentation

### Phase 7: Activity Logging (`src/events/`)
1. Pydantic schemas for four event categories: `audit`, `debug`, `usage`, `insight`
2. Centralized redaction helper (headers, body keys, URL query params)
3. Async JSONL writer with buffered queue and per-month file rotation
4. Retention cleanup (delete files older than `MCP_LOG_RETENTION_DAYS`, never the current month)
5. Public `Recorder` API integrated by tools, gateway, and auth modules
6. Per-API payload depth controls in `config/api_configs.json` (`metadata`/`summary`/`full`)
7. Logs are operator-only — **not** exposed via any MCP tool

## Critical Files (status)
- `src/server.py` — MCP server entry point ✓ **implemented**
- `src/config.py` — API config loader with `${VAR}` substitution ✓ **implemented**
- `src/tools/spec.py`, `src/tools/registry.py`, `src/tools/builtin.py` — tool registry + `list_apis` ✓ **implemented**
- `src/auth/oauth.py` — OAuth 2.0 + PKCE flow ✓ **implemented**
- `src/auth/credentials.py` — Keyring-backed credential store with concurrent-refresh lock ✓ **implemented**
- `src/events/` — Activity logging (schemas, writers, retention, recorder) ✓ **implemented**
- `src/gateway/api_client.py` — Generic REST/GraphQL client (planned, Phase 4)
- `src/gateway/handlers.py` — Response normalization (reuses `src/events/redaction.py`) (planned, Phase 4)
- `src/models/data_models.py` — Pydantic models for responses (planned, Phase 5)
- `src/tools/mcp_tools.py` — `fetch_data`/`send_data`/`execute_graphql`/`get_status` (planned, Phase 5)
- `config/api_configs.json` — API configuration template (gitignored runtime file; example committed at `config/api_configs.example.json`)
- `tests/events/` — 27 passing unit tests for `src/events/` ✓ **implemented**
- `tests/auth/` — 49 passing unit tests for `src/auth/` ✓ **implemented**
- `tests/test_config.py`, `tests/test_server.py`, `tests/tools/` — Phase 2 unit tests ✓ **implemented**
- `tests/gateway/`, `tests/integration/` — integration tests for remaining phases (planned, Phase 4 + 6)

## Technology Stack

See [`README.md` § Tech Stack](../README.md). Source of truth for runtime versions:
[`requirements.txt`](../requirements.txt).

## Verification Plan
1. **Unit tests** per module (auth, gateway, handlers, tools)
2. **Integration tests** with mock external APIs
3. **Manual testing**:
   - Start MCP server (`python -m src.server`)
   - Configure a sample API in `config/api_configs.json`
   - Invoke `fetch_data` from Claude → verify OAuth popup opens
   - Verify token persists in keyring; second call skips popup
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
- **Multi-tenant** — separate credential stores per user
