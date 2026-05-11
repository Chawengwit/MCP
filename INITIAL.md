# Feature Request

> One feature per file. Run `/generate-prp INITIAL.md` then review the produced PRP and
> run `/execute-prp PRPs/{...}.md`.
>
> **This file is a DELTA**: it does NOT repeat info already in [docs/plan.md](docs/plan.md)
> or [CLAUDE.md](CLAUDE.md). It captures only what's specific or deeper than the roadmap.

---

## STATUS

**Phases 1–8 shipped.** GitHub OAuth integration verified end-to-end against Claude
Desktop **and** Codex CLI/Desktop over stdio. HTTP transport verified end-to-end
against Codex Desktop with Bearer-token auth (288 tests passing).

| Phase | PRP | Status |
|-------|-----|--------|
| 3 — Authentication | [PRPs/phase3-auth.md](PRPs/phase3-auth.md) | ✅ Done |
| 4 — API Gateway | [PRPs/phase4-gateway.md](PRPs/phase4-gateway.md) | ✅ Done |
| 5 — Tools & Integration | [PRPs/phase5-tools.md](PRPs/phase5-tools.md) | ✅ Done |
| 6 — Testing & Documentation | [PRPs/phase6-testing-docs.md](PRPs/phase6-testing-docs.md) | ✅ Done |
| **GitHub OAuth integration** | (no PRP — direct edit) | ✅ Done — `scripts/oauth_login.py` |
| 8 — HTTP Transport | [PRPs/phase8-http-transport.md](PRPs/phase8-http-transport.md) | ✅ Done — `src/transport/`, `MCP_TRANSPORT` switch |
| **9 — OAuth Provider (Claude.ai Custom Connector)** | (TBD) | 🔵 Next |

**Total test count: 288 passing** (will grow with Phase 9's OAuth Provider tests).

---

## NEXT FEATURE — Phase 9: OAuth Provider for Claude.ai Custom Connector

### FEATURE

Turn the MCP Data Gateway into a standards-compliant **OAuth 2.0 Authorization
Server** so that **Claude.ai's "Add custom connector"** flow can register, obtain
tokens, and call our MCP HTTP endpoint as a multi-tenant client. Each Claude.ai
end-user authenticates as **themselves** to an upstream Service API and gets a
per-user MCP access token mapped to their Service-API session.

The shift is from a single shared `MCP_HTTP_BEARER_TOKEN` (Phase 8) to **per-user
OAuth tokens**, where the user's identity comes from the upstream Service API.

#### High-level flow

```
1. User in Claude.ai → "Add Custom Connector" → enters MCP HTTPS URL
2. Claude.ai → GET  /.well-known/oauth-authorization-server   (RFC 8414 discovery)
3. Claude.ai → POST /register                                 (RFC 7591 Dynamic Client Registration)
4. Claude.ai opens browser → GET /authorize?...               (PKCE, S256)
5. MCP renders HTML consent form asking for Service API credentials
6. User submits api_key + secret_key
7. MCP → POST {service_api}/auth (api_key+secret_key) → session_id + user_info
8. MCP stores per-user mapping (encrypted at rest) + issues auth_code
9. Redirect to Claude.ai with auth_code
10. Claude.ai → POST /token (code + PKCE verifier) → mcp_access_token
11. All subsequent /mcp calls use Bearer mcp_access_token
12. On /mcp call, MCP looks up mcp_access_token → user's session_id → calls Service API
13. When session_id expires, MCP transparently re-auths with stored api_key+secret_key
```

### AUTH STRATEGY

**Plan B-Pragmatic** (decided after design review with operator):

- MCP is a **full OAuth 2.0 Authorization Server** to Claude.ai (mandatory — Claude.ai
  requires standard OAuth + PKCE + Dynamic Client Registration).
- MCP is a **session-login client** to the upstream Service API (Plan B). It calls
  the Service API's existing `POST /auth` endpoint with `api_key + secret_key`,
  receives a `session_id` plus user metadata, and uses the `session_id` as the
  bearer for subsequent Service API calls.
- The Service API is **NOT modified** in Phase 9. (Phase 10 may upgrade the Service
  API to a real OAuth Authorization Server using `league/oauth2-server` in PHP.)

Sample Service API auth response (operator-provided, for reference):
```json
{
  "status": "success",
  "code": 201,
  "data": {
    "expire": 1781068453,
    "user_type": "user",
    "company_group": "taximail",
    "company_app_type": "default",
    "app_package": "free",
    "salepage_type": "default",
    "member_config": null,
    "session_id": "zzzzzzc2b60c1559bbbbbb.."
  }
}
```

The `session_id` field is what subsequent API calls must carry. The `expire` field
is a Unix timestamp; MCP must refresh the session before it elapses.

### ACCEPTANCE CRITERIA

#### OAuth Authorization Server endpoints (MCP exposes)

- [ ] `GET /.well-known/oauth-authorization-server` returns RFC 8414 metadata
      with `authorization_endpoint`, `token_endpoint`, `registration_endpoint`,
      `code_challenge_methods_supported: ["S256"]`, `grant_types_supported:
      ["authorization_code", "refresh_token"]`, and `response_types_supported:
      ["code"]`.
- [ ] `POST /register` (RFC 7591) accepts a JSON body with `redirect_uris` and
      `client_name`, returns a generated `client_id` (and optional `client_secret`
      for confidential clients — Claude.ai is a public client, so `none` is fine).
      Stored in SQLite.
- [ ] `GET /authorize` validates `client_id`, `redirect_uri`, `code_challenge`,
      `code_challenge_method=S256`, `state`, `response_type=code`. On valid
      request, renders an HTML consent form titled with the client name.
- [ ] `POST /authorize/consent` accepts the consent form submission
      (`api_key`, `secret_key`, the original `state`/`client_id`/`redirect_uri`/
      `code_challenge`), calls the Service API's `/auth` endpoint, stores
      credentials + session, generates an `authorization_code`, and redirects
      to `redirect_uri?code=...&state=...`.
- [ ] `POST /token` accepts:
      - `grant_type=authorization_code`, `code`, `code_verifier`, `client_id`,
        `redirect_uri` → returns `access_token` (opaque, 64-char hex), `token_type:
        Bearer`, `expires_in`, optional `refresh_token`.
      - `grant_type=refresh_token`, `refresh_token`, `client_id` → returns a new
        `access_token`. (Refresh token rotation: optional in Phase 9.)
- [ ] PKCE verification: `BASE64URL(SHA256(verifier)) == code_challenge` using
      `hashlib.sha256` + `base64.urlsafe_b64encode` (stripped `=`).
- [ ] Authorization codes are single-use, 10-minute TTL, deleted after exchange.

#### Bearer middleware behavior (replaces Phase 8's static-token check)

- [ ] When an OAuth client_id has been registered, the `/mcp` endpoint requires
      `Authorization: Bearer <mcp_access_token>` where the token resolves in the
      SQLite token store. Missing/invalid → 401 with
      `WWW-Authenticate: Bearer realm="mcp", error="invalid_token"` and a body
      including the `resource_metadata` URL (per MCP Authorization spec).
- [ ] **Back-compat**: if `MCP_HTTP_BEARER_TOKEN` is set in env AND no OAuth
      clients are registered, the Phase 8 static-token behavior remains active.
      This lets existing Codex Desktop setups keep working until they migrate.
- [ ] When BOTH are configured, OAuth tokens take precedence; the static bearer
      is accepted as a fallback (for operator/admin use) — logged with
      `auth_source: "static_bearer"` so it's auditable.

#### Per-user multi-tenant tool execution

- [ ] `ToolContext` gains a `user_id: str | None` and `service_session: SessionInfo
      | None` field. Tools that hit the Service API read the session from context,
      not from a process-wide singleton.
- [ ] The middleware looks up `mcp_access_token → (user_id, service_session)` from
      SQLite, attaches them to the `ToolContext`, and passes through to tool
      handlers.
- [ ] When `service_session.expire - now < 60s`, the gateway transparently calls
      `Service API /auth` again with the stored `api_key + secret_key` to refresh.
      Refresh is **per-user-locked** (per-user `asyncio.Lock`) to prevent thundering
      herd — mirrors `src/auth/credentials.py:33-34`.

#### Storage (SQLite at `data/oauth_provider.db`)

- [ ] Schema (one DDL file under `src/oauth_provider/schema.sql`):
      - `clients(client_id PK, client_name, redirect_uris JSON, created_at)`
      - `authorization_codes(code PK, client_id, user_id, redirect_uri,
         code_challenge, expires_at)`
      - `access_tokens(token PK, client_id, user_id, expires_at, refresh_token,
         created_at, last_used_at)`
      - `service_sessions(user_id PK, company_group, encrypted_api_key,
         encrypted_secret_key, session_id, session_expire, user_type, app_package,
         updated_at)`
- [ ] `api_key` and `secret_key` are encrypted at rest with **Fernet** (symmetric,
      `cryptography` library). Key source: env var `MCP_OAUTH_ENCRYPTION_KEY`
      (32-byte urlsafe base64). Server refuses to start if unset when OAuth is
      enabled.
- [ ] All writes go through a single `OAuthStore` class (mirror
      `src/events/recorder.py` public-class pattern). All public methods are
      async; SQLite I/O wrapped in `asyncio.to_thread`.

#### Service API client (`src/auth/service_api.py`)

- [ ] New module `authenticate(base_url, api_key, secret_key) -> SessionInfo`
      that POSTs to the configured login endpoint and parses the response shown
      above. Returns a Pydantic model.
- [ ] Errors mapped to `AuthRequiredError` / `AuthFailedError` (existing classes
      from Phase 3) so the tool surface stays consistent.
- [ ] Configurable per-API: the login endpoint, response field paths, and
      expire-field path live in `config/api_configs.json` under a new auth type
      `"session_login"` (alongside existing `oauth2`/`bearer`/`api_key`).

```json
{
  "auth": {
    "type": "session_login",
    "login_path": "/auth/login",
    "login_method": "POST",
    "credentials": ["api_key", "secret_key"],
    "session_id_field": "data.session_id",
    "session_expire_field": "data.expire",
    "user_id_field": "data.company_group",
    "session_header": "Authorization",
    "session_format": "Bearer {session_id}"
  }
}
```

#### Activity logging — user attribution

- [ ] `audit`, `usage`, and `insight` records gain an optional
      `user_id: str | None` field. Tools called over OAuth populate it from
      `ToolContext.user_id`. Tools called over the static bearer leave it `None`.
- [ ] The `debug` log is request-level (no user concept) — unchanged shape.
- [ ] No raw `session_id`, `api_key`, `secret_key`, or `mcp_access_token` is
      ever written to any log. Redaction list extended in `src/events/redaction.py`.

#### Discovery + WWW-Authenticate (MCP Authorization spec compliance)

- [ ] When the `/mcp` endpoint returns 401, the response includes
      `WWW-Authenticate: Bearer resource_metadata="<server>/.well-known/oauth-protected-resource"`
      per the MCP Authorization spec, and the body is a JSON error envelope
      (`{"error": "invalid_token", "error_description": "..."}`).
- [ ] `GET /.well-known/oauth-protected-resource` returns
      `{"resource": "<server>/mcp", "authorization_servers": ["<server>"]}`.

### EDGE CASES

- **Loopback vs public bind**: OAuth Provider mode requires a public bind for
  Claude.ai to reach us. Phase 8's loopback guard must be relaxed: when OAuth
  is enabled (i.e., `MCP_OAUTH_ENCRYPTION_KEY` is set), public bind is allowed
  *without* `MCP_HTTP_BEARER_TOKEN`, because per-user OAuth tokens are the auth.
- **Multiple Claude.ai users on same `api_key`**: not blocked. Two MCP tokens
  may map to the same `(api_key, secret_key)`. Refresh logic must handle this
  (the per-user lock keys on `user_id`, which is the Service API's user
  identifier — two MCP tokens with the same user_id share one lock).
- **Token theft / revocation**: out of UI scope for Phase 9. CLI command
  `python -m scripts.oauth_admin revoke --user-id <id>` deletes the token and
  service session.
- **PKCE verifier longer than RFC limits**: validate per RFC 7636 (43-128 chars,
  URL-safe). Reject early with `invalid_request`.
- **Concurrent session refresh**: per-user `asyncio.Lock` (see "Things to Watch
  For" in CLAUDE.md). The first concurrent caller refreshes; the rest wait.
- **HTTPS off for local dev (ngrok)**: ngrok provides HTTPS at the public side.
  Locally, MCP serves HTTP on 127.0.0.1:8080. Claude.ai will refuse non-HTTPS
  remote URLs — operator must use ngrok or a similar tunnel for testing.
- **Discovery probing before registration**: Claude.ai may fetch
  `.well-known/oauth-authorization-server` without a registered client. The
  endpoint must work unauthenticated.

### OUT OF SCOPE

- Adding a real OAuth Authorization Server **to the upstream Service API** (PHP,
  `league/oauth2-server`) — Phase 10.
- A web admin UI for revocation, client management, or token inspection — CLI
  only.
- Refresh token rotation (RFC 6749 §6) — basic refresh works; rotation is
  optional optimisation.
- Multi-tenant credential isolation **beyond** the OAuth Provider — the Service
  API itself enforces per-user permissions, so MCP doesn't need to re-check.
- Federated identity (Google/Microsoft OAuth at the MCP level) — Service API
  already offers these to users via its own UI; MCP just consumes the resulting
  api_key/secret_key.
- Replacing keychain for the legacy GitHub OAuth flow — that path stays. Phase 9
  adds the SQLite-based path *alongside* it. They coexist; the auth type in
  `api_configs.json` decides which is used per API.

### REFERENCE PATTERNS

- **Public-class + `from_env()`**: `src/events/recorder.py` — the new
  `OAuthStore` and `OAuthProvider` classes should follow this exact shape.
- **Per-`id` `asyncio.Lock`**: `src/auth/credentials.py:33-34` — copy the
  refresh-race pattern for session refresh.
- **Pydantic v2 schemas**: `src/events/schemas.py` for `SessionInfo`,
  `ClientRegistration`, `AuthorizationCode`, `AccessToken`.
- **Redaction helpers**: `src/events/redaction.py` — extend the redaction list,
  do NOT reimplement.
- **HTTP middleware (pure-ASGI, not BaseHTTPMiddleware)**:
  `src/transport/http.py` — the new OAuth middleware replaces/wraps the existing
  Bearer middleware following the same pattern.
- **Loopback guard pattern**: `src/transport/http.py` — extend, don't replace.

### NEW DEPENDENCIES

Add to `requirements.txt`:
- `cryptography` — Fernet encryption for at-rest api_key/secret_key.
- `itsdangerous` — signed session cookies for the consent flow's state
  parameter (optional; alternative is a signed JWT-like format hand-rolled
  with `hmac`).

NOT added:
- `authlib` — heavier than needed; we implement the small Authorization Server
  surface ourselves following the same minimal style as `src/transport/`.
- `python-jose` / `pyjwt` — we use opaque tokens, not JWT.

### VALIDATION GATES (run during `/execute-prp`)

```bash
ruff check src/ scripts/ tests/ --fix
ruff format src/ scripts/ tests/
mypy src/ scripts/ tests/
pytest tests/ -v          # baseline 288 passing; Phase 9 adds new tests
```

### EXPECTED DELIVERABLES

- `src/oauth_provider/__init__.py` — public API exports
- `src/oauth_provider/schemas.py` — Pydantic models
- `src/oauth_provider/schema.sql` — SQLite DDL
- `src/oauth_provider/store.py` — `OAuthStore` (CRUD on SQLite)
- `src/oauth_provider/encryption.py` — Fernet wrapper, `MCP_OAUTH_ENCRYPTION_KEY`
- `src/oauth_provider/discovery.py` — `/.well-known/oauth-authorization-server` +
  `/.well-known/oauth-protected-resource`
- `src/oauth_provider/register.py` — `POST /register`
- `src/oauth_provider/authorize.py` — `GET /authorize` + `POST /authorize/consent`
- `src/oauth_provider/token.py` — `POST /token`
- `src/oauth_provider/middleware.py` — Bearer validation + ToolContext injection
- `src/oauth_provider/templates/consent.html` — minimal HTML consent form
- `src/auth/service_api.py` — Service API session-login client
- `src/transport/http.py` — wire OAuth endpoints + middleware into the ASGI app
- `src/tools/context.py` — add `user_id`, `service_session` fields
- `src/events/schemas.py` — add optional `user_id` to audit/usage/insight
- `src/events/redaction.py` — extend redaction list
- `scripts/oauth_admin.py` — operator CLI: list clients, revoke tokens, dump
  user sessions (no secrets in output)
- `config/api_configs.example.json` — example for new `"session_login"` auth type
- `tests/oauth_provider/test_discovery.py`
- `tests/oauth_provider/test_register.py`
- `tests/oauth_provider/test_authorize.py`
- `tests/oauth_provider/test_token.py`
- `tests/oauth_provider/test_store.py`
- `tests/oauth_provider/test_encryption.py`
- `tests/oauth_provider/test_middleware.py`
- `tests/auth/test_service_api.py`
- `tests/integration/test_oauth_full_flow.py` — end-to-end via TestClient
- `requirements.txt` (+ `cryptography`, optionally `itsdangerous`)
- `.env.example` — `MCP_OAUTH_ENCRYPTION_KEY`, `MCP_OAUTH_DB_PATH`,
  `MCP_OAUTH_ISSUER` (canonical server URL for discovery responses)
- `README.md` — new "OAuth Provider / Claude.ai Custom Connector" section
- `CLAUDE.md` — new subsection in "Transport Selection" describing OAuth mode

---

## ARCHIVE — Phase 8 feature delta (now shipped)

See git history at commit `1dafca7` for the full Phase 8 INITIAL.md content.
Phase 8 added the HTTP transport with static Bearer auth, the loopback guard,
and the `MCP_TRANSPORT` selector. Phase 9 extends the HTTP transport with
OAuth Provider capability without removing the static-bearer path.
