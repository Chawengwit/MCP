# MCP Data Gateway

> **Status: Early Development.** Project scaffolding (config, dependencies, docs) is in place. Source code under `src/` has not yet been implemented — see the [Development Roadmap](#development-roadmap) for current progress. The implementation plan is at [`docs/plan.md`](docs/plan.md).

A Python-based **Model Context Protocol (MCP) server** that acts as a unified data gateway, enabling Claude (and other MCP clients) to send and receive data across multiple external APIs through a single, secure interface.

## Overview

This MCP server provides:
- **Generic data handling** for multiple data types
- **Generic API gateway** supporting any REST or GraphQL endpoint
- **OAuth 2.0 authentication** with automatic browser-based login flow
- **Secure credential storage** using system keyring
- **Foundation for MCP App** evolution in the future

## Features

| Feature | Description |
|---------|-------------|
| Multi-API Support | Connect to any number of external services through unified configuration |
| REST + GraphQL | Native support for both REST and GraphQL APIs |
| OAuth 2.0 | Full authorization code flow with automatic browser popup |
| Token Refresh | Automatic token refresh and re-authentication when expired |
| Secure Storage | Credentials stored encrypted via system keyring |
| Generic Data Models | Flexible schemas to handle any data shape |
| Auto-Authentication | Tools automatically prompt login when needed |

## Architecture

```
MCP/
├── src/
│   ├── server.py              # MCP server entry point
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── oauth.py           # OAuth 2.0 flow handler with popup
│   │   └── credentials.py     # Secure credential storage (keyring)
│   ├── gateway/
│   │   ├── __init__.py
│   │   ├── api_client.py      # Generic REST/GraphQL HTTP client
│   │   └── handlers.py        # Request/response transformation
│   ├── models/
│   │   ├── __init__.py
│   │   └── data_models.py     # Generic Pydantic data models
│   └── tools/
│       ├── __init__.py
│       └── mcp_tools.py       # MCP tool definitions for Claude
├── config/
│   └── api_configs.json       # API service configurations
├── tests/                     # Unit and integration tests
├── .env.example               # Environment variables template
├── .gitignore                 # Excludes secrets and build artifacts
├── requirements.txt           # Python dependencies
└── README.md                  # This file
```

### Module Responsibilities

#### Core MCP Server (`src/server.py`)
- Initializes the MCP server using the Python `mcp` SDK
- Registers tools (`fetch_data`, `send_data`, `execute_graphql`, etc.)
- Handles tool execution lifecycle and error responses

#### Authentication (`src/auth/`)
- **oauth.py**: OAuth 2.0 authorization code flow with automatic browser popup. Spins up a local HTTP callback server to receive the auth code. Supports multiple providers (Google, GitHub, custom).
- **credentials.py**: Secure storage of access/refresh tokens via the system keyring. Handles token validation and expiration.

#### API Gateway (`src/gateway/`)
- **api_client.py**: Generic async HTTP client supporting REST (GET/POST/PUT/DELETE) and GraphQL (queries/mutations). Handles Bearer tokens, API keys, Basic auth.
- **handlers.py**: Normalizes responses across different APIs and parses GraphQL errors separately from HTTP errors.

#### MCP Tools (`src/tools/mcp_tools.py`)
| Tool | Description |
|------|-------------|
| `fetch_data` | GET data from a configured API (auto-OAuth if required) |
| `send_data` | POST/PUT data to a configured API (auto-OAuth if required) |
| `execute_graphql` | Run a GraphQL query or mutation (auto-OAuth if required) |
| `list_apis` | List all configured API services |
| `get_status` | Show authentication and connection status |

## Authentication Flow

When Claude calls a tool that requires authentication:

```
1. Claude invokes tool (e.g., fetch_data)
        ↓
2. MCP checks credentials in keyring
        ↓
3a. Valid token  →  Proceed with API call
3b. Missing/Expired  →  Open browser popup for OAuth
        ↓
4. User logs in via browser
        ↓
5. Local callback server receives auth code
        ↓
6. Exchange auth code for access token
        ↓
7. Store token in keyring
        ↓
8. Resume original tool call
```

## Tech Stack

- **Python 3.10+**
- **mcp** — Model Context Protocol Python SDK
- **httpx** — Async HTTP client (REST + GraphQL)
- **keyring** — Cross-platform secure credential storage
- **pydantic** — Data validation and modeling
- **python-dotenv** — Environment variable management

## Setup

### Prerequisites
- Python 3.10 or higher
- pip or uv (recommended)

### Installation

```bash
# Clone the repository
cd /Users/chawengwit/Documents/MCP

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
# Edit .env with your OAuth credentials and API settings
```

### Configuration

#### 1. Environment Variables (`.env`)
```bash
# OAuth credentials (per provider)
OAUTH_CLIENT_ID=your_client_id
OAUTH_CLIENT_SECRET=your_client_secret
OAUTH_REDIRECT_URI=http://localhost:8765/callback

# Server settings
MCP_LOG_LEVEL=INFO              # DEBUG | INFO | WARN | ERROR
MCP_LOG_FILE=                   # Optional path to log file (default: stderr only)
MCP_DEBUG=false                 # Enable verbose request tracing
MCP_MAX_RESPONSE_BYTES=1048576  # Response size cap (1 MB default)
OAUTH_CALLBACK_PORT=8765
```

#### 2. API Configurations (`config/api_configs.json`)
```json
{
  "apis": {
    "example_api": {
      "base_url": "https://api.example.com",
      "type": "rest",
      "auth": {
        "method": "oauth2",
        "provider": "custom",
        "authorize_url": "https://auth.example.com/oauth/authorize",
        "token_url": "https://auth.example.com/oauth/token",
        "scopes": ["read", "write"]
      },
      "endpoints": {
        "get_users": {"method": "GET", "path": "/users"},
        "create_user": {"method": "POST", "path": "/users"}
      }
    }
  }
}
```

## Usage

### Running the MCP Server

```bash
python -m src.server
```

### Connecting to Claude Code

Add this configuration to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "data-gateway": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/Users/chawengwit/Documents/MCP"
    }
  }
}
```

### Example Interactions

Once connected, Claude can:

- **List configured APIs**: "Show me the available API services"
- **Fetch data**: "Get the user list from example_api"
- **Send data**: "Create a new record in example_api with this data..."
- **Execute GraphQL**: "Run this GraphQL query against my API..."

The first time Claude uses a tool requiring authentication, your browser will open automatically for OAuth login.

## Response Format

All MCP tools return structured JSON for consistent parsing.

**Success:**
```json
{
  "data": <api response>,
  "metadata": { "source": "...", "endpoint": "...", "timestamp": "...", "duration_ms": 142 }
}
```

**Error:**
```json
{
  "error": { "code": "AUTH_REQUIRED", "message": "...", "details": { ... } }
}
```

**Standard error codes:** `AUTH_REQUIRED`, `AUTH_FAILED`, `API_NOT_CONFIGURED`, `ENDPOINT_NOT_FOUND`, `RATE_LIMITED`, `UPSTREAM_ERROR`, `VALIDATION_ERROR`, `RESPONSE_TOO_LARGE`.

JSON/text responses larger than `MCP_MAX_RESPONSE_BYTES` are **truncated and returned as success** with `metadata.truncated: true` plus pagination cursors. Only binary or streaming payloads emit `RESPONSE_TOO_LARGE` (they can't be safely truncated). Binary data is base64-encoded with `content_type` metadata. GraphQL responses surface both `data` and `errors` so partial successes remain usable.

See [CLAUDE.md](CLAUDE.md#response-format-conventions) for full details.

## Debugging

### Quick Diagnostics
| Symptom | Try |
|---------|-----|
| Tool hangs on first call | Check `OAUTH_CALLBACK_PORT` is free |
| `keyring.errors.NoKeyringError` | Install `keyrings.alt` (headless Linux) |
| 401 after working previously | Delete keyring entry, re-authenticate |
| GraphQL "succeeds" but no data | Check `errors[]` in response body |
| Truncated response | Use pagination or raise `MCP_MAX_RESPONSE_BYTES` |

### Enabling Debug Mode
```bash
MCP_DEBUG=true MCP_LOG_LEVEL=DEBUG python -m src.server
```
This dumps full HTTP exchanges (with secrets redacted) to `stderr`. **Never to `stdout`** — `stdout` carries the MCP JSON-RPC protocol stream.

### Logging Notes
- All logs go to `stderr` (or optional `MCP_LOG_FILE`).
- Tokens, API keys, `Authorization` headers, and credentials are auto-redacted before logging.
- Logs are structured JSON (one event per line) for easy parsing with `jq`.

See [CLAUDE.md](CLAUDE.md#debug--logging-strategy) for full debugging strategy.

## Development Roadmap

### Phase 1: Project Setup
- [x] `requirements.txt` with pinned dependencies
- [x] `.gitignore` for secrets and caches
- [x] `.env.example` documenting environment variables
- [ ] Initialize `src/` package structure
- [ ] Initial `config/api_configs.json` template

### Phase 2: Core MCP Server
- [ ] MCP server initialization
- [ ] Tool schema definitions
- [ ] Logging and error handling

### Phase 3: Authentication
- [ ] OAuth 2.0 authorization code flow
- [ ] Local callback HTTP server
- [ ] Keyring-based token storage
- [ ] Token refresh logic

### Phase 4: API Gateway
- [ ] Generic REST client
- [ ] GraphQL query/mutation support
- [ ] Multi-auth method support
- [ ] Request/response handlers

### Phase 5: Tools & Integration
- [ ] Implement `fetch_data` tool
- [ ] Implement `send_data` tool
- [ ] Implement `execute_graphql` tool
- [ ] Implement `list_apis` and `get_status` tools

### Phase 6: Testing & Polish
- [ ] Unit tests per module
- [ ] Integration tests with mock APIs
- [ ] Configuration examples
- [ ] User documentation

## Future Enhancements

- **MCP App**: Standalone web interface as a frontend on top of this gateway
- **Persistent Storage**: SQLite/PostgreSQL for data history and audit logs
- **Rate Limiting**: Per-API rate limiting and request queuing
- **Caching**: Response caching with configurable TTL
- **Multi-Tenant**: Support multiple users with separate credential stores
- **Webhooks**: Receive data via incoming webhooks
- **Data Transformation Pipelines**: Chain transformations across APIs

## Security

- All credentials stored in OS-level secure keyring (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux)
- `.env` file excluded from version control via `.gitignore`
- OAuth uses standard authorization code flow (no implicit grant)
- Tokens never logged or exposed in error messages
- Local callback server only listens on `localhost` and only during the OAuth flow

## License

TBD

## Contributing

This project is in early development. Contribution guidelines will be added once the core implementation is stable.
