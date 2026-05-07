# MCP Data Gateway

> **Status: Early Development.** Activity logging (`src/events/`) is implemented and tested. Core MCP server, auth, gateway, and tools (Phases 2–5) are not yet implemented — see the [Development Roadmap](#development-roadmap). The implementation plan is at [`docs/plan.md`](docs/plan.md).

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

Files marked **(planned)** are not yet implemented. Files without that marker exist today.

```
MCP/
├── src/
│   ├── server.py              # MCP server entry point (planned)
│   ├── auth/                  # OAuth 2.0 + keyring (planned)
│   │   ├── oauth.py
│   │   └── credentials.py
│   ├── gateway/               # REST/GraphQL HTTP client (planned)
│   │   ├── api_client.py
│   │   └── handlers.py
│   ├── models/                # Pydantic data models (planned)
│   │   └── data_models.py
│   ├── tools/                 # MCP tool definitions (planned)
│   │   └── mcp_tools.py
│   └── events/                # Activity logging (implemented ✓)
│       ├── schemas.py         # Pydantic models (audit/debug/usage/insight)
│       ├── redaction.py       # Sensitive data redaction
│       ├── retention.py       # Per-month file rotation cleanup
│       ├── writers.py         # Async JSONL writer + queue
│       └── recorder.py        # Public Recorder API
├── config/
│   ├── api_configs.json       # API service configurations (planned)
│   └── api_configs.example.json  # Template (committed)
├── docs/
│   └── plan.md                # Implementation plan / roadmap
├── tests/
│   └── events/                # Unit tests for src/events/ (27 cases, 51 collected w/ parametrize)
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
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install runtime dependencies
pip install -r requirements.txt

# (Optional) Install dev/test dependencies for running pytest
pip install -r requirements-dev.txt

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

# Activity logging (operator-only)
MCP_LOG_DIR=./logs
MCP_LOG_RETENTION_DAYS=365
MCP_LOG_AUDIT_ENABLED=true
MCP_LOG_DEBUG_ENABLED=true
MCP_LOG_USAGE_ENABLED=true
MCP_LOG_INSIGHT_ENABLED=true
MCP_LOG_FLUSH_INTERVAL_SEC=5
MCP_LOG_BUFFER_SIZE=100
```

See [`.env.example`](.env.example) for the full annotated template.

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

### Running Tests

```bash
# After installing requirements-dev.txt:
pytest tests/

# Run a specific test file with verbose output
pytest tests/events/test_writers.py -v

# Currently 27 tests for src/events/ — all passing.
```

### Running the MCP Server (planned)

```bash
python -m src.server
```
> Phase 2 — `src/server.py` is not yet implemented.

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

All MCP tools return structured JSON: `{data, metadata}` on success, `{error}` on failure.
Large responses truncate (success + cursor) where safe; binary/streaming emit
`RESPONSE_TOO_LARGE`. GraphQL surfaces partial-success (data + errors).

For the full spec — exact field shapes, the error-code table, the truncation rule, and
the GraphQL handling — see
[`CLAUDE.md` § Response Format Conventions](CLAUDE.md).

## Debugging

To enable verbose tracing:

```bash
MCP_DEBUG=true MCP_LOG_LEVEL=DEBUG python -m src.server
```

All logs go to **stderr** (stdout is reserved for the MCP JSON-RPC protocol). Secrets are
auto-redacted.

For the full debug/logging strategy, env-var reference, and the symptom→cause→fix table,
see [`CLAUDE.md` § Debug & Logging Strategy](CLAUDE.md).

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
| 1 | Project Setup | ✅ mostly done |
| 2 | Core MCP Server | ⏳ pending |
| 3 | Authentication (OAuth + keyring) | ⏳ pending |
| 4 | API Gateway (REST + GraphQL) | ⏳ pending |
| 5 | Tools & Integration | ⏳ pending |
| 6 | Testing & Polish | ⏳ ongoing |
| 7 | Activity Logging (`src/events/`) | ✅ done — 27 test cases (51 collected with parametrization) |

Per-phase deliverables and verification plan: [`docs/plan.md`](docs/plan.md).
Future scalability ideas (web UI, multi-tenant, caching, etc.) live in
[`docs/plan.md` § Future Scalability](docs/plan.md).

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
