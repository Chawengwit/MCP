name: "PRP — Phase 9: OAuth Provider for Claude.ai Custom Connector"
description: |
  Turn the MCP Data Gateway into an OAuth 2.0 Authorization Server so Claude.ai's
  "Add custom connector" flow can register, obtain per-user tokens, and call the
  HTTP `/mcp` endpoint. Each Claude.ai end-user authenticates as themselves to an
  upstream Service API via `api_key + secret_key` (Plan B — Service API is **not**
  modified in this phase). Tokens map to encrypted Service-API sessions in SQLite.

## Principles

1. **Context only as needed** — cite files, don't restate them. Hard cap 200 lines.
2. **Validation gates are executable** — `ruff`, `mypy`, `pytest` commands as written.
3. **Mirror, don't invent** — `src/events/` is the reference implementation.
4. **Follow [CLAUDE.md](../CLAUDE.md)** — global rules take precedence.

---

## Goal

Ship a standards-compliant OAuth 2.0 Authorization Server **inside** the existing
HTTP transport so that:

- `Claude.ai → Add custom connector → <our HTTPS URL>` succeeds end-to-end.
- The MCP server discovers itself via `/.well-known/oauth-authorization-server`,
  accepts Dynamic Client Registration, runs the authorization-code-with-PKCE flow,
  and issues opaque per-user access tokens.
- Each access token resolves to a `(user_id, service_session)` pair via a SQLite
  store; tools execute against the **per-user** Service API session, not a shared
  keychain token.
- The Phase 8 `MCP_HTTP_BEARER_TOKEN` path keeps working as a fallback (back-compat
  for existing Codex Desktop installs).
- Audit / usage / insight logs gain an optional `user_id` field — multi-tenant
  observability without leaking secrets.

## Why

- Phase 9 of [docs/plan.md](../docs/plan.md). Closes the gap that prevents Claude.ai
  (cloud) from talking to this server — Claude.ai requires OAuth, not static Bearer.
- Sets up the multi-tenant token model. Future phases (per-user keychain isolation,
  full OAuth Server on the PHP Service API) build on this surface.
- Service API stays untouched — fastest credible path to "Claude.ai can use my
  data" without rewriting PHP auth.

## What

User-visible behaviour:

- Operator runs `MCP_TRANSPORT=http MCP_OAUTH_ENCRYPTION_KEY=... python -m src.server`,
  exposes via ngrok/TLS, pastes URL into Claude.ai's Add Connector dialog.
- Claude.ai discovers the server, registers, and redirects the user to an HTML
  consent form (served by our server). User pastes their `api_key` + `secret_key`,
  hits Authorize.
- All subsequent tool calls from that Claude.ai session run **as that user**
  against the Service API.
- Existing Codex Desktop setup (Phase 8 static Bearer) keeps working unchanged.

### Success Criteria

- [ ] `GET /.well-known/oauth-authorization-server` returns RFC 8414 metadata
      (issuer, authorization_endpoint, token_endpoint, registration_endpoint,
      `code_challenge_methods_supported: ["S256"]`, `grant_types_supported:
      ["authorization_code","refresh_token"]`, `response_types_supported: ["code"]`,
      `token_endpoint_auth_methods_supported: ["none"]` for public clients).
- [ ] `GET /.well-known/oauth-protected-resource` returns
      `{"resource": "<issuer>/mcp", "authorization_servers": ["<issuer>"]}`.
- [ ] `POST /register` (RFC 7591) accepts `{redirect_uris, client_name}`, returns
      `{client_id, client_id_issued_at, redirect_uris, client_name}`. Persisted
      in SQLite. No client_secret required (Claude.ai is a public client).
- [ ] `GET /authorize` validates all params (`client_id`, `redirect_uri`,
      `code_challenge`, `code_challenge_method=S256`, `state`, `response_type=code`).
      Renders consent HTML. Invalid params → 400 with `{"error":"invalid_request"}`.
- [ ] `POST /authorize/consent` calls the configured Service API auth endpoint
      with submitted `api_key + secret_key`, stores the session encrypted, generates
      a 10-min single-use authorization code, redirects to `redirect_uri?code=&state=`.
      Service API failure → re-render form with an error banner (no stack trace).
- [ ] `POST /token` (grant_type=authorization_code) verifies PKCE
      `BASE64URL(SHA256(verifier)) == code_challenge`, issues opaque access token
      (`secrets.token_urlsafe(48)`), optional refresh token, returns standard
      OAuth JSON body. Codes are single-use (delete on exchange).
- [ ] `POST /token` (grant_type=refresh_token) issues a new access token and
      invalidates the old (no rotation needed beyond delete-old).
- [ ] On `/mcp`, the Bearer middleware: if token exists in `access_tokens` table,
      load `(user_id, service_session)`, attach to `ToolContext`, pass through.
      If the static `MCP_HTTP_BEARER_TOKEN` matches, attach `user_id=None,
      auth_source="static_bearer"`. Otherwise → 401 with
      `WWW-Authenticate: Bearer resource_metadata="<issuer>/.well-known/oauth-protected-resource"`.
- [ ] Per-user Service-API session is refreshed transparently when
      `session_expire - now < 60s`, guarded by a per-`user_id` `asyncio.Lock`
      (mirror `src/auth/credentials.py:33-34, 113-122`).
- [ ] `api_key` and `secret_key` are encrypted at rest with **Fernet**. Key in
      `MCP_OAUTH_ENCRYPTION_KEY` (32-byte urlsafe base64). Startup fails loudly
      if OAuth is enabled and the key is missing.
- [ ] New auth type `"session_login"` recognised by `ApiAuthConfig`. Configurable
      `login_path`, `login_method`, `credentials`, `session_id_field`,
      `session_expire_field`, `user_id_field`, `session_header`, `session_format`.
- [ ] `AuditEvent`, `UsageEvent`, `InsightEvent` gain optional `user_id: str | None`.
      Populated from `ToolContext.user_id` in tool handlers. `DebugEvent` unchanged
      (request-level).
- [ ] **No** raw `session_id`, `api_key`, `secret_key`, `mcp_access_token`,
      `refresh_token`, or `client_secret` appears in any log line. Verify by grep
      after smoke test.
- [ ] All existing 288 tests still pass. Phase 9 adds ≥ 40 new tests.
- [ ] `ruff check src/ scripts/ tests/`, `ruff format --check src/ scripts/ tests/`,
      and `mypy src/ scripts/ tests/` are clean.

## All Needed Context

> Hard cap: ≤ 200 lines. Cite files with line ranges; never paste full source.

### Documentation & References

```yaml
- url: https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization
  why: Canonical MCP Authorization spec — defines /.well-known endpoints,
       WWW-Authenticate header shape, and Bearer token expectations.
  critical: On 401 the server MUST return
            `WWW-Authenticate: Bearer resource_metadata="<url>"`.
            The `resource_metadata` value points clients to the protected-
            resource discovery document, NOT the AS discovery document.

- url: https://datatracker.ietf.org/doc/html/rfc8414
  why: OAuth 2.0 Authorization Server Metadata. The discovery document shape.
  critical: `issuer` MUST be an HTTPS URL with no query/fragment. For local
            ngrok testing use the ngrok HTTPS URL as issuer.

- url: https://datatracker.ietf.org/doc/html/rfc7591
  why: Dynamic Client Registration. POST /register payload + response.
  critical: For public clients (no client_secret) the response includes
            `client_id_issued_at` (epoch seconds). Claude.ai is a public client.

- url: https://datatracker.ietf.org/doc/html/rfc7636
  why: PKCE. code_challenge / code_verifier validation.
  critical: code_verifier MUST be 43-128 chars from
            `[A-Z][a-z][0-9]-._~`. Reject early at /authorize and /token.
            Comparison uses `secrets.compare_digest` on bytes.

- url: https://cryptography.io/en/latest/fernet/
  why: Fernet symmetric encryption for at-rest api_key/secret_key.
  critical: Key MUST be 32 url-safe base64-encoded bytes. Generate via
            `cryptography.fernet.Fernet.generate_key()`. Treat the key as a
            top-secret env var; rotation is out of scope for Phase 9.

- file: src/events/recorder.py
  why: Public-class + classmethod from_env() pattern — mirror for OAuthStore
       and OAuthProvider classes.
  lines: 22-57, 165-171

- file: src/events/schemas.py
  why: Pydantic v2 + Literal categories + `model_config = {"extra": "forbid"}`.
       Mirror for new schemas (ClientRegistration, AuthorizationCode,
       AccessToken, ServiceSession, ConsentRequest).
  lines: 22-26, 29-39, 70-78, 90-96

- file: src/events/redaction.py
  why: Central redaction helpers. EXTEND DEFAULT_BODY_KEYS to include
       new sensitive field names; do NOT reimplement.
  lines: 6-30, 33-60

- file: src/auth/credentials.py
  why: Per-`id` asyncio.Lock + refresh-near-expiry pattern. Copy verbatim
       for ServiceSessionStore.
  lines: 15 (REFRESH_BUFFER_SEC), 33-40 (class docstring), 66-85 (get with lock),
         113-122 (_lock_for — NOTE the "no await" comment)

- file: src/transport/http.py
  why: Pure-ASGI middleware pattern (NOT BaseHTTPMiddleware), dispatcher,
       loopback guard, `secrets.compare_digest`. Extend, don't replace.
  lines: 8-27 (composition docstring — keep ordering), 62-74 (loopback guard),
         77-117 (bearer middleware — base to wrap), 142-193 (build_app
         dispatcher), 219-270 (run_http entry)

- file: src/tools/context.py
  why: ToolContext dataclass — add user_id and service_session fields here.
  lines: 12-26

- file: src/config.py
  why: ApiAuthConfig Pydantic model — extend for `session_login` fields.
  lines: 13-25, 47-53

- doc: CLAUDE.md
  section: "Response Format Conventions", "Activity Logging (src/events/)",
           "Security Rules (Strict)", "Things to Watch For"
  critical: All logs to stderr; tokens never in logs; redaction is centralised
            in src/events/redaction.py; UTC for all timestamps and DB rows.

- doc: CLAUDE.md
  section: "Transport Selection" + "HTTP-mode invariants"
  critical: Pure-ASGI middleware (NOT BaseHTTPMiddleware). uvicorn owns signals.
            stdout discipline preserved (logs stderr-only).
```

### Desired Codebase tree (files to add/modify)

Existing files modified: `src/config.py` (extend `ApiAuthConfig`), `src/tools/context.py`,
`src/events/{schemas,recorder,redaction}.py`, `src/transport/http.py`, `.env.example`,
`README.md`, `CLAUDE.md`, `config/api_configs.example.json`. All Phase 1–8 modules
untouched in functionality (only additive).

```
src/oauth_provider/
  __init__.py               — public exports (OAuthStore, OAuthProvider)
  schema.sql                — DDL: clients, authorization_codes, access_tokens, service_sessions
  schemas.py                — Pydantic models (ClientRegistration, AuthorizationCode, AccessToken, ServiceSession, ConsentForm)
  encryption.py             — Fernet wrapper; key from MCP_OAUTH_ENCRYPTION_KEY; raises if missing
  store.py                  — OAuthStore: async CRUD via asyncio.to_thread; pragmas (foreign_keys, journal_mode=WAL)
  service_session.py        — ServiceSessionStore: refresh-near-expiry with per-user_id Lock (mirrors credentials.py)
  discovery.py              — /.well-known/oauth-authorization-server, /.well-known/oauth-protected-resource
  register.py               — POST /register handler
  authorize.py              — GET /authorize (render) + POST /authorize/consent (process)
  token.py                  — POST /token (authorization_code + refresh_token grants)
  pkce.py                   — verify_code_challenge(verifier, challenge, method)
  middleware.py             — OAuth-aware Bearer middleware (wraps Phase 8 middleware)
  routes.py                 — assemble OAuth ASGI dispatcher; called from src/transport/http.py
  templates/consent.html    — minimal HTML consent form (Jinja-free; format string + html.escape)

src/auth/
  service_api.py            — authenticate(config, api_key, secret_key) -> SessionInfo

src/transport/
  http.py                   — integrate OAuth routes + middleware; preserve Phase 8 fallback

src/tools/
  context.py                — add user_id, service_session, auth_source fields

src/events/
  schemas.py                — add optional user_id to AuditEvent, UsageEvent, InsightEvent
  redaction.py              — extend DEFAULT_BODY_KEYS with session_id, mcp_access_token, code_verifier

src/config.py               — extend ApiAuthConfig with session_login fields

scripts/oauth_admin.py      — CLI: list-clients, revoke-token, dump-sessions (no secrets)

tests/oauth_provider/
  __init__.py
  test_discovery.py         — metadata shape, issuer formatting
  test_register.py          — happy path + invalid redirect_uri
  test_authorize.py         — param validation, consent form render
  test_consent.py           — Service API success/failure paths
  test_token.py             — authorization_code, refresh_token, PKCE rejection
  test_pkce.py              — challenge/verifier verification
  test_store.py             — CRUD + foreign keys + WAL pragmas
  test_encryption.py        — encrypt/decrypt round-trip, key-missing failure
  test_middleware.py        — token-lookup, fallback to static bearer, 401 header
  test_service_session.py   — refresh-near-expiry + lock concurrency

tests/auth/
  test_service_api.py       — login parsing, error mapping, redaction-safe logging

tests/integration/
  test_oauth_full_flow.py   — end-to-end discovery → register → authorize → token → /mcp via Starlette TestClient

config/api_configs.example.json   — add a "session_login" example block

.env.example                — MCP_OAUTH_ENCRYPTION_KEY, MCP_OAUTH_DB_PATH, MCP_OAUTH_ISSUER

README.md                   — new "OAuth Provider / Claude.ai Custom Connector" section

CLAUDE.md                   — new "OAuth Provider mode" subsection under Transport Selection
```

### Known Gotchas

```python
# Pure-ASGI middleware only — see src/transport/http.py:5-27. BaseHTTPMiddleware
# buffers and breaks SSE; new OAuth middleware MUST be `async def m(scope, receive, send)`.

# Per-user_id Lock — see src/auth/credentials.py:113-122 ("no await" invariant).
# Two coroutines refreshing the same user's session must serialise on ONE Lock
# instance, or Service API /auth gets called twice and burns quota.

# Fernet key = 32 url-safe base64 bytes. `Fernet(key)` raises otherwise.
# Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
# Do NOT hash/derive — use the env var verbatim.

# SQLite + asyncio: sqlite3 connections are thread-local. Either open a fresh
# connection per write inside the `asyncio.to_thread` callable, or hold one
# writer connection behind an `asyncio.Lock`. Enable `journal_mode=WAL` either
# way so reads stay concurrent. Sharing a connection across to_thread without
# serialisation eventually raises ProgrammingError.

# HTML escaping in consent.html: `html.escape()` every user-controllable value
# (client_name, redirect_uri, error). Do NOT add Jinja for one form.

# `state` is opaque — echo unchanged on success and error (RFC 6749 §4.1.2.1).
# Do NOT parse or validate it.

# `redirect_uri` exact-match: literal byte comparison after %-decoding
# (RFC 6749 §3.1.2.4). No wildcards, no scheme-stripping. Mismatch → invalid_request.

# 401 body: per MCP Authorization spec the WWW-Authenticate header carries
# `resource_metadata="<url>"` (quoted). Body JSON
# `{"error":"invalid_token","error_description":"..."}` — short, no token echo.

# Service API response uses dotted-path lookup (e.g. `data.session_id`). Missing
# field → AUTH_FAILED, not 500.

# Loopback guard relaxation: when OAuth is enabled, public bind WITHOUT
# MCP_HTTP_BEARER_TOKEN is permitted (per-user tokens replace static auth).
# Tests must cover the 3-state matrix below:
#   (a) loopback + no token + OAuth off  → OK
#   (b) public + no token + OAuth off    → LoopbackGuardError
#   (c) public + no token + OAuth on     → OK
```

## Implementation Blueprint

### Data models (Pydantic v2, all `extra=forbid`)

- `ClientRegistration` — client_id (str, generated), client_name, redirect_uris
  (list[str], min 1), created_at (datetime UTC).
- `AuthorizationCode` — code (str, generated), client_id, user_id, redirect_uri,
  code_challenge, code_challenge_method (Literal["S256"]), expires_at (datetime).
- `AccessToken` — token (str, generated), client_id, user_id, expires_at,
  refresh_token (str | None), created_at, last_used_at.
- `ServiceSession` — user_id (PK), company_group, encrypted_api_key (bytes),
  encrypted_secret_key (bytes), session_id (str), session_expire (int),
  user_type, app_package, updated_at.
- `SessionInfo` (returned by `src/auth/service_api.py.authenticate`) — pydantic
  view of the Service API response. Includes raw `session_id` for in-memory use
  only; serialisation strips it via `model_dump(exclude={"session_id"})`.
- `ConsentForm` — incoming POST body schema for `POST /authorize/consent`.

### Tasks (in order)

```yaml
Task 1 — Foundation: encryption + Pydantic schemas + DDL:
  CREATE src/oauth_provider/encryption.py:
    - Fernet wrapper; load key from MCP_OAUTH_ENCRYPTION_KEY env at construction
    - Raises ConfigurationError if missing
  CREATE src/oauth_provider/schemas.py:
    - All Pydantic models listed above
    - MIRROR: src/events/schemas.py:22-26 (BaseModel + extra=forbid)
  CREATE src/oauth_provider/schema.sql:
    - Four tables; PRAGMA foreign_keys=ON; UNIQUE on token/code columns
  KEY DECISION: opaque tokens (not JWT) → revocation is delete-row.

Task 2 — Storage: OAuthStore + ServiceSessionStore:
  CREATE src/oauth_provider/store.py:
    - OAuthStore class; from_env(); init_db() runs DDL idempotently
    - Methods: register_client, get_client, save_authorization_code,
      consume_authorization_code, save_access_token, get_access_token,
      delete_access_token, save_refresh_token (or reuse access_token table)
    - MIRROR: src/events/recorder.py:22-57 (public class + from_env + start/stop)
    - All public methods async; sqlite3 via asyncio.to_thread
  CREATE src/oauth_provider/service_session.py:
    - ServiceSessionStore class with per-user_id asyncio.Lock
    - get(user_id): returns SessionInfo, refreshes if near expiry
    - MIRROR: src/auth/credentials.py:30-122 verbatim — copy the structure,
      adapt fields. Keep the "no await in _lock_for" comment.

Task 3 — PKCE + discovery:
  CREATE src/oauth_provider/pkce.py:
    - verify_code_challenge(verifier, challenge, method="S256") -> bool
    - Uses hashlib.sha256 + base64.urlsafe_b64encode (strip "=")
    - Compares with secrets.compare_digest
  CREATE src/oauth_provider/discovery.py:
    - Two ASGI handlers; build response from MCP_OAUTH_ISSUER env

Task 4 — Dynamic Client Registration:
  CREATE src/oauth_provider/register.py:
    - POST /register handler (ASGI callable)
    - Validates redirect_uris (https or http+loopback), generates client_id,
      persists via OAuthStore.register_client
    - Returns RFC 7591 response shape

Task 5 — Authorize + consent:
  CREATE src/oauth_provider/templates/consent.html:
    - One HTML file, no JS frameworks. Form posts to /authorize/consent.
  CREATE src/oauth_provider/authorize.py:
    - GET /authorize: validate params, html.escape, render template
    - POST /authorize/consent: call src.auth.service_api.authenticate(),
      store ServiceSession, generate AuthorizationCode, redirect 302 to
      `redirect_uri?code=...&state=...`
    - On Service API failure: re-render template with sanitised error

Task 6 — Token endpoint:
  CREATE src/oauth_provider/token.py:
    - POST /token (Content-Type: application/x-www-form-urlencoded per RFC)
    - grant_type=authorization_code: verify PKCE, exchange single-use code,
      issue access_token + refresh_token
    - grant_type=refresh_token: rotate access_token, keep or rotate refresh_token

Task 7 — Service API client:
  CREATE src/auth/service_api.py:
    - authenticate(api_config, api_key, secret_key) async function
    - httpx.AsyncClient POST with timeout from ApiLimitsConfig
    - Parse response via dotted-path lookup; map errors to AuthFailedError
    - Never log api_key/secret_key/session_id values; only event names

Task 8 — Config extension:
  MODIFY src/config.py:
    - Extend ApiAuthConfig with: login_path, login_method, credentials,
      session_id_field, session_expire_field, user_id_field,
      session_header, session_format
    - These are all Optional[str] / list[str] — only required when type ==
      "session_login"
  UPDATE config/api_configs.example.json: add a session_login example

Task 9 — Middleware + ToolContext:
  MODIFY src/tools/context.py:
    - Add: user_id: str | None = None, service_session: ServiceSession | None
      = None, auth_source: Literal["oauth","static_bearer"] | None = None
  CREATE src/oauth_provider/middleware.py:
    - oauth_aware_bearer_middleware(app, store, static_token): pure-ASGI
    - Token resolution order: (1) lookup in access_tokens; (2) match static
      bearer; (3) 401 with MCP Authorization spec headers
    - Attaches resolved fields into the ASGI scope under a private key
      (e.g. scope["state"]["mcp_user"]) for downstream tools to read
  KEY DECISION: do NOT modify Phase 8's bearer_auth_middleware. Wrap it.

Task 10 — Routes wiring + transport integration:
  CREATE src/oauth_provider/routes.py:
    - build_oauth_dispatcher(store) — returns an ASGI callable that handles
      /.well-known/*, /register, /authorize, /authorize/consent, /token
    - Unknown paths fall through to the outer dispatcher (returns False)
  MODIFY src/transport/http.py:
    - In build_app(), compose: outer = oauth_middleware(oauth_dispatcher +
      fallthrough(mcp_dispatcher))
    - Relax loopback guard when OAuth is enabled
    - Preserve Phase 8 fallback (static token via env)
  KEY DECISION: middleware order — OAuth middleware MUST run before
    dispatch so /mcp gets the resolved user; discovery endpoints bypass it.

Task 11 — Logging + redaction:
  MODIFY src/events/schemas.py:
    - Add user_id: str | None = None to AuditEvent, UsageEvent, InsightEvent
  MODIFY src/events/redaction.py:
    - Extend DEFAULT_BODY_KEYS: session_id, mcp_access_token, code_verifier,
      authorization_code, secret_key, refresh_token (already there)
  MODIFY src/events/recorder.py:
    - record_audit/record_usage/record_insight accept user_id kwarg, default
      None; pass to event constructors
  UPDATE all tool handlers under src/tools/ that currently call record_*:
    - Pass user_id=ctx.user_id (read from ToolContext)

Task 12 — Operator CLI + docs:
  CREATE scripts/oauth_admin.py:
    - subcommands: list-clients, list-tokens, revoke-token, list-sessions
    - No secrets in output (mask api_key/secret_key/tokens)
  UPDATE .env.example: add the three new MCP_OAUTH_* variables
  UPDATE README.md: new section with Claude.ai connection instructions + ngrok
  UPDATE CLAUDE.md: subsection under Transport Selection

Task 13 — Tests (write incrementally throughout, finalise here):
  CREATE all tests/oauth_provider/test_*.py files listed above
  CREATE tests/auth/test_service_api.py
  CREATE tests/integration/test_oauth_full_flow.py
  KEY DECISION: integration test uses httpx + Starlette TestClient + a fake
    Service API on a tmp port (or `respx` for httpx mocking).
```

### Integration Points

```yaml
RECORDER:
  source: src.events.Recorder
  changes: record_audit / record_usage / record_insight gain user_id kwarg
  callers: all existing tools — pass ToolContext.user_id

CONFIG:
  source: src.config.load_api_configs
  new auth type: "session_login" (alongside oauth2, bearer, api_key)
  new env vars (introduced):
    - MCP_OAUTH_ENCRYPTION_KEY (required when OAuth enabled; 32-byte url-safe base64)
    - MCP_OAUTH_DB_PATH (default: data/oauth_provider.db)
    - MCP_OAUTH_ISSUER (canonical https URL of this server — used in discovery)
    - MCP_OAUTH_ENABLED (default: auto-on when MCP_OAUTH_ENCRYPTION_KEY is set)

TRANSPORT:
  source: src.transport.http.build_app, run_http
  changes: compose OAuth dispatcher + middleware; relax loopback guard when OAuth on
  back-compat: MCP_HTTP_BEARER_TOKEN still accepted as fallback

LOGGING:
  destination: stderr only
  level: MCP_LOG_LEVEL (default INFO)
  new redaction keys: session_id, mcp_access_token, code_verifier, authorization_code
```

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ scripts/ tests/ --fix
ruff format src/ scripts/ tests/
mypy src/ scripts/ tests/
```

Zero errors. Do not silence with `# type: ignore` / `# noqa` without a one-line
comment justifying it.

### Level 2 — Unit tests

```bash
pytest tests/oauth_provider/ -v
pytest tests/auth/test_service_api.py -v
pytest tests/ -v          # full suite — 288 baseline + new tests
```

Required test categories per new module:

- **happy path** (valid input → expected output)
- **invalid input → specific exception class** (not bare `Exception`)
- **async path with start/stop in try/finally** (where applicable)
- **secret-omission**: dump the serialised model / DB row / log line and assert
  no `api_key`, `secret_key`, `session_id`, `code_verifier`, `mcp_access_token`,
  `refresh_token`, or raw `client_secret` value appears

PKCE-specific tests:

- generate a known verifier → assert
  `BASE64URL(SHA256(verifier)).rstrip("=") == challenge`
- length boundaries: 42 chars (reject), 43 (accept), 128 (accept), 129 (reject)

### Level 3 — Smoke test

```bash
# 1. Generate a Fernet key once
KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 2. Boot HTTP + OAuth, on loopback so no TLS needed
MCP_TRANSPORT=http \
MCP_OAUTH_ENCRYPTION_KEY="$KEY" \
MCP_OAUTH_ISSUER="http://127.0.0.1:8080" \
python -m src.server &
SERVER_PID=$!
sleep 1

# 3. Discovery
curl -s http://127.0.0.1:8080/.well-known/oauth-authorization-server | python -m json.tool

# 4. Dynamic registration
curl -s -X POST http://127.0.0.1:8080/register \
  -H "Content-Type: application/json" \
  -d '{"client_name":"smoke","redirect_uris":["http://127.0.0.1/cb"]}' | python -m json.tool

# 5. 401 on /mcp without token, with the right WWW-Authenticate header
curl -i -s -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}' | head -5

kill $SERVER_PID
```

Assertions: discovery body has `code_challenge_methods_supported: ["S256"]`,
`/register` returns a `client_id`, `/mcp` 401 carries
`WWW-Authenticate: Bearer resource_metadata="..."`. stdout from `python -m
src.server` is empty (logs went to stderr).

## MCP Security Checklist

The implementing agent MUST verify each item before marking the feature complete.
Each check is grep-able so it can be re-run on the diff.

- [ ] **No secrets in logs (any level).**
      `grep -riE 'authorization:|bearer |client_secret|access_token|api_key|secret_key|session_id|code_verifier' logs/`
      → must return zero matches with non-`<redacted>` values.
- [ ] **All HTTP traffic logging routes through `src/events/redaction.py`** —
      `redact_headers`, `redact_body`, `redact_url`. New sensitive keys are
      added to `DEFAULT_BODY_KEYS`; no per-call reimplementation.
- [ ] **At-rest secrets**: `api_key` and `secret_key` are stored Fernet-encrypted
      in SQLite. Plaintext appears only in-memory during the consent POST
      handler and immediately after decryption inside the gateway call site.
- [ ] **Authorization codes are single-use**: `consume_authorization_code`
      deletes the row inside the same transaction that returns it.
- [ ] **PKCE method is `S256` only** — reject `plain` with `invalid_request`.
- [ ] **redirect_uri exact match** at `/authorize` and `/token` (literal byte
      comparison after %-decoding).
- [ ] **Tool responses contain zero auth fields.**
      `grep -E '"api_key"|"secret_key"|"session_id"|"mcp_access_token"|"refresh_token"' <serialized response>` →
      zero matches.
- [ ] **Pydantic validates every endpoint body.** No raw `dict[str, Any]` reaches
      business logic without a Pydantic model in between.
- [ ] **Error messages do not leak internals** — no filesystem paths, env var
      names, stack traces, or Service API error bodies surfaced verbatim to the
      consent HTML response.
- [ ] **New pip deps flagged**: `cryptography` is added in this PRP; nothing
      else without an explicit note in the commit message.
- [ ] **Stdout has zero non-protocol output**; logs go to stderr only.

## Risks

1. **MCP Authorization spec version drift.** The 2025-03-26 spec is the version
   referenced here, but Claude.ai's connector implementation may pin a newer
   draft with different `WWW-Authenticate` semantics or extra metadata fields.
   *Recovery:* before wiring middleware, re-fetch the latest spec via WebSearch
   and confirm: (a) exact `WWW-Authenticate` header format, (b) any newly
   required discovery metadata fields, (c) whether `oauth-protected-resource`
   is at the spec'd path. Adjust discovery.py and middleware.py to match the
   newest stable spec.

2. **SQLite + asyncio concurrency pitfalls.** sqlite3 objects are not
   thread-safe by default; sharing a connection across `asyncio.to_thread`
   calls without serialisation will eventually raise
   `ProgrammingError: SQLite objects created in a thread can only be used in
   that same thread`.
   *Recovery:* either (a) open a new connection per write inside the
   `to_thread` callable (simplest, slight perf cost — fine for OAuth volume),
   or (b) hold one writer connection guarded by a single
   `asyncio.Lock`; either way, enable `journal_mode=WAL` for concurrent reads.
   Test with `pytest -x -k concurrency` exercising 10+ parallel writes.

3. **Service API quota burn from refresh races.** Without the per-user_id
   `asyncio.Lock` (Task 2), two concurrent tool calls for the same user with
   an expired session each hit `Service API /auth`, doubling auth quota usage
   and potentially tripping rate limits.
   *Recovery:* mirror `src/auth/credentials.py:113-122` exactly — the "no
   await between dict get and set" invariant. Cover with a test that races 10
   coroutines on a single user_id and asserts `service_api.authenticate` was
   called exactly once.

## Final Checklist

- [ ] `ruff check src/ scripts/ tests/` clean
- [ ] `ruff format src/ scripts/ tests/ --check` clean
- [ ] `mypy src/ scripts/ tests/` clean
- [ ] `pytest tests/ -v` — all pass (288 existing + new Phase 9 tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] Smoke test passes (Level 3) — discovery, register, 401 with correct
      `WWW-Authenticate`
- [ ] Acceptance criteria from "Success Criteria" — all checked
- [ ] `.env.example`, `README.md`, `CLAUDE.md` updated with new env vars and
      OAuth Provider section
- [ ] `config/api_configs.example.json` has a `session_login` example block
- [ ] `scripts/oauth_admin.py` runs `list-clients`, `list-tokens`,
      `revoke-token`, `list-sessions` against a real SQLite file without
      printing any plaintext secret
