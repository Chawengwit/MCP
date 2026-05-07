# PRP — Phase 3: Authentication (`src/auth/`)

## Goal

Implement OAuth 2.0 authorization code flow (`src/auth/oauth.py`) and keyring-backed
secure credential storage (`src/auth/credentials.py`). Provide an async, public API that
Phase 5 tools will use to auto-authenticate every API call without exposing tokens or
secrets to logs/responses.

## Why

- This is **Phase 3** of [docs/plan.md](../docs/plan.md).
- **Phase 5 tools** (`fetch_data`, `send_data`, `execute_graphql`) cannot make a single
  authenticated call without `Credentials.get(api_id)`.
- **Phase 4 gateway** injects auth headers obtained from `credentials.get(...)` into every
  outbound request.
- Credentials are the most security-sensitive subsystem — getting this right unblocks all
  remaining phases safely.

## What

### `src/auth/oauth.py` — OAuth 2.0 authorization code flow

- Class `OAuth` with async `start_flow(config) -> TokenInfo` and
  `refresh(config, refresh_token) -> TokenInfo`.
- **Scope:** Phase 3 handles ONLY `ApiAuthConfig.type == "oauth2"` configs. Bearer-token
  configs (`type == "bearer"` with `token_env`) and unauthenticated APIs (`auth: null`)
  are NOT processed here — Phase 5 tools branch on `auth.type` and read tokens from
  `os.environ[token_env]` directly for the bearer case.
- Read `OAuthConfig` (provider, client_id, client_secret, authorize_url, token_url,
  scopes) from `ApiAuthConfig` in `src/config.py` when `type == "oauth2"`.
- PKCE support: generate `state` + `code_verifier` + `code_challenge` (S256).
- Local HTTP callback server: bind to `127.0.0.1` only (NOT `localhost` — some browsers
  treat them as different origins for OAuth state tracking), port from
  `OAUTH_CALLBACK_PORT` (default 8765). Server lifetime bounded by the active flow —
  close immediately after the auth code is captured. The redirect URI registered with
  the OAuth provider must use `http://127.0.0.1:{port}/callback` exactly.
- Open browser via `webbrowser.open()`.
- Exchange auth code for tokens (POST to `token_url`).
- Return `TokenInfo(access_token, refresh_token, expires_at, token_type)`.

### `src/auth/credentials.py` — Keyring-backed storage

- Class `Credentials` with async methods:
  - `get(api_id: str, required: bool = True) -> TokenInfo | None`
  - `peek(api_id: str) -> TokenInfo | None` — **read-only**, no refresh, no OAuth trigger
  - `store(api_id: str, tokens: TokenInfo) -> None`
  - `clear(api_id: str) -> None`
- `get()` checks expiry — if `expires_at - now() < 300s`, call `oauth.refresh(...)` and
  re-store. **Triggers OAuth flow** if no token exists and `required=True`.
- `peek()` returns whatever is currently stored (or None) without modification — used by
  `get_status` in Phase 5 to report state without side effects.
- Use `keyring.get_password()` / `keyring.set_password()` / `keyring.delete_password()`.
- One service name per process (e.g. `mcp-data-gateway`); username is `api_id`.
- On `keyring.errors.NoKeyringError`, raise `CredentialStorageError` with a message
  pointing the user at `keyrings.alt`. Do NOT silently fall back.
- **Concurrent refresh protection**: one `asyncio.Lock` per `api_id`. First waiter
  performs refresh; others wait and read the fresh token.

### `src/auth/__init__.py`

Export: `OAuth`, `Credentials`, `TokenInfo`, `OAuthConfig`, `CredentialStorageError`,
`AuthError`, `AuthRequiredError`.

### Success Criteria

- [ ] OAuth authorize → callback → token exchange flow works (mocked in tests).
- [ ] PKCE state + code_challenge generated and validated.
- [ ] Token expiry < 5 min triggers automatic refresh on next `get()`.
- [ ] Keyring unavailable → `CredentialStorageError` with actionable message.
- [ ] Concurrent `get()` for same `api_id` with expired token → exactly one refresh.
- [ ] Callback server binds to `127.0.0.1` only and closes after token exchange.
- [ ] No tokens, refresh tokens, secrets, or auth codes appear in any log line.
- [ ] All existing 27 events tests still pass.
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest tests/` all green.

---

## All Needed Context

### Documentation & References

```yaml
- doc: CLAUDE.md
  section: "Security Rules (Strict)"
  critical: Never log tokens, secrets, or full request/response bodies. Never write credentials to plaintext files.

- doc: CLAUDE.md
  section: "Standard Error Codes"
  critical: AUTH_REQUIRED, AUTH_FAILED — use these exact codes when surfacing errors upward.

- doc: CLAUDE.md
  section: "Common Failure Modes"
  critical: Callback port conflict → OAUTH_CALLBACK_PORT env var. Headless Linux → keyrings.alt.

- file: src/events/recorder.py
  why: Public-class pattern with from_env() classmethod and explicit start/stop lifecycle
  lines: 22–60

- file: src/events/redaction.py
  why: REUSE for any logging in oauth.py — never reimplement
  lines: 1–80

- file: src/config.py
  why: ApiAuthConfig already defines provider, client_id, authorize_url, token_url, scopes
  lines: 13–24

- url: https://datatracker.ietf.org/doc/html/rfc7636
  why: PKCE — code_verifier, code_challenge (S256), state parameter
  critical: code_verifier must be 43–128 chars, base64url-encoded; code_challenge = base64url(sha256(verifier))

- url: https://docs.python.org/3/library/asyncio-sync.html#asyncio.Lock
  why: One Lock per api_id; acquire before refresh check, release after store
  critical: asyncio.Lock, NOT threading.Lock

- url: https://keyring.readthedocs.io/en/latest/
  why: keyring.get_password / set_password / delete_password / errors.NoKeyringError
  critical: NoKeyringError raised lazily — wrap first call, not import

- url: https://docs.python.org/3/library/webbrowser.html
  why: webbrowser.open(url, new=2) opens in new tab if possible
  critical: Returns True/False; do NOT block on it
```

### Current Codebase

```
src/
├── config.py                 ← ApiAuthConfig already defined; reuse it
├── events/                   ← reference implementation (mirror this)
├── server.py                 ← will pass Credentials to tools in Phase 5
└── tools/                    ← Phase 5 callers
```

### Desired Codebase

```
src/auth/
├── __init__.py               ← export public API
├── oauth.py                  ← OAuth class, TokenInfo dataclass, PKCE helpers
└── credentials.py            ← Credentials class, CredentialStorageError

tests/auth/
├── __init__.py
├── conftest.py               ← shared fixtures (mock keyring, mock httpx, mock browser)
├── test_oauth.py             ← flow, PKCE, refresh, callback bind address
└── test_credentials.py       ← get/store/clear, auto-refresh, concurrent refresh, no-keyring
```

### Known Gotchas (Phase 3 specific)

```python
# CRITICAL — Concurrent refresh race
# Two tools call credentials.get(api_id) simultaneously with expired token.
# Without a lock: both refresh, second refresh may invalidate the first refresh_token.
# Fix: dict[str, asyncio.Lock] keyed by api_id; lock created lazily on first get().
# File: src/auth/credentials.py — acquire lock BEFORE expiry check, release in finally.

# CRITICAL — Keyring unavailable on headless Linux
# import keyring succeeds; keyring.get_password() raises NoKeyringError lazily.
# Fix: catch on first call (not on import). Raise CredentialStorageError with text:
#   "No keyring backend available. Install 'keyrings.alt' (pip install keyrings.alt)
#    or set MCP_CREDENTIALS_STORAGE=file (not yet implemented)."
# File: src/auth/credentials.py — wrap first keyring call in try/except.

# CRITICAL — OAuth callback port conflict
# Default 8765 may be in use. EADDRINUSE on bind.
# Fix: read OAUTH_CALLBACK_PORT env (default 8765). Catch OSError(EADDRINUSE);
#   raise OAuthError("Callback port {n} in use. Set OAUTH_CALLBACK_PORT=<free port>.")
# File: src/auth/oauth.py — start_flow() reads env, binds, handles error.

# CRITICAL — Callback server lifetime
# Server must close after token exchange. Leaving it open is a security hole.
# Fix: use asyncio.start_server inside an `async with` or try/finally; close in finally.
# File: src/auth/oauth.py — _run_callback_server returns (auth_code, state) then closes.

# CRITICAL — PKCE code_verifier length
# Must be 43–128 chars from [A-Z][a-z][0-9]-._~ (RFC 7636 §4.1).
# Fix: secrets.token_urlsafe(64) gives ~86 chars, all valid.
# File: src/auth/oauth.py — _generate_pkce() uses secrets.token_urlsafe.

# CRITICAL — Never log tokens
# webbrowser.open() takes the authorize_url which contains client_id (low risk) but
# the token POST and response contain access_token/refresh_token. Never log these.
# Fix: redact via src/events/redaction.py before any debug log; INFO level only logs
#   "OAuth flow started for provider <name>" with no URL or token data.
# File: src/auth/oauth.py — every logger.info/debug call goes through redaction helpers.
```

---

## Implementation Blueprint

### Data Models

```python
# src/auth/oauth.py
class TokenInfo(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: float          # absolute Unix timestamp
    token_type: str = "Bearer"
    scope: str | None = None

class OAuthConfig(BaseModel):
    provider: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    redirect_uri: str | None = None   # default: http://127.0.0.1:{port}/callback

# src/auth/credentials.py
class CredentialStorageError(RuntimeError): ...
class AuthError(RuntimeError): ...
class AuthRequiredError(AuthError): ...
```

### Tasks (in order)

```yaml
Task 1 — Skeleton + exceptions:
  CREATE src/auth/__init__.py:
    - Re-export public API
  CREATE src/auth/oauth.py:
    - TokenInfo, OAuthConfig models
    - OAuthError exception
    - Stub OAuth class with start_flow / refresh signatures
  CREATE src/auth/credentials.py:
    - CredentialStorageError, AuthError, AuthRequiredError
    - Stub Credentials class with get / store / clear signatures

Task 2 — PKCE + URL builder:
  ADD to src/auth/oauth.py:
    - _generate_pkce() -> (verifier, challenge, state)
    - _build_authorize_url(config, challenge, state, redirect_uri) -> str
  KEY DECISION: Use secrets.token_urlsafe for verifier (43+ chars, RFC-safe alphabet).

Task 3 — Local callback server:
  ADD to src/auth/oauth.py:
    - async _run_callback_server(port, expected_state) -> auth_code
    - Use asyncio.start_server bound to 127.0.0.1
    - Parse query string for ?code=...&state=...
    - Validate state matches expected_state (CSRF protection)
    - Close server in finally block
  KEY DECISION: Reject any request whose state mismatches — return 400 + error page.

Task 4 — Token exchange + refresh:
  ADD to src/auth/oauth.py:
    - async _exchange_code(config, code, verifier, redirect_uri) -> TokenInfo
    - async refresh(config, refresh_token) -> TokenInfo
    - Both use httpx.AsyncClient with 30s timeout
    - Convert response to TokenInfo; expires_at = time.time() + expires_in
  MIRROR pattern from: src/events/recorder.py async method style

Task 5 — Full OAuth flow orchestration:
  ADD to src/auth/oauth.py:
    - async start_flow(config) -> TokenInfo
      1. _generate_pkce()
      2. open browser at _build_authorize_url(...)
      3. _run_callback_server(port, state) -> code
      4. _exchange_code(config, code, verifier, redirect_uri) -> TokenInfo
  KEY DECISION: start_flow accepts the OAuthConfig directly (no global state).

Task 6 — Credentials class (keyring + lock):
  COMPLETE src/auth/credentials.py:
    - __init__: keyring service name, dict[str, asyncio.Lock]
    - async get(api_id, required=True):
        acquire per-api lock
        read keyring; if missing and required → raise AuthRequiredError
        if expires_at - now < 300 → call oauth.refresh, re-store
        return TokenInfo
    - async store(api_id, tokens): serialize to JSON, keyring.set_password
    - async clear(api_id): keyring.delete_password
    - On NoKeyringError on first use → CredentialStorageError with actionable message
  KEY DECISION: Serialize TokenInfo as JSON for keyring storage (single string slot).

Task 7 — Tests:
  CREATE tests/auth/conftest.py:
    - mock_keyring fixture (in-memory dict)
    - mock_httpx fixture for token endpoint (use httpx.MockTransport)
    - mock_webbrowser fixture (returns True without opening a real browser)
    - patched_callback_server fixture: monkeypatches OAuth._run_callback_server to
      return a pre-set (auth_code, state) tuple immediately, so the test does NOT
      depend on a real loopback HTTP listener. The fixture stores the expected_state
      passed in and asserts it matches the state generated by _generate_pkce.
  CREATE tests/auth/test_oauth.py:
    - PKCE: verifier length 43–128, challenge = b64url(sha256(verifier)), state random
    - URL builder: includes all required params, encodes scopes correctly
    - Callback server (real socket, fast test): bind address is 127.0.0.1
      (assert on socket.getsockname()[0])
    - Callback server: state mismatch → 400, no code returned
    - Token exchange: POST shape, parses access_token/refresh_token/expires_in,
      computes expires_at = time.time() + expires_in
    - refresh(): uses grant_type=refresh_token
    - Full start_flow happy path: uses patched_callback_server fixture (NOT a real
      loopback listener) so the test injects a pre-set (auth_code, state) tuple and
      verifies the rest of the flow (URL build → callback → token exchange) end-to-end
  CREATE tests/auth/test_credentials.py:
    - get with valid token returns it
    - get with expired token triggers refresh → re-store → return fresh token
    - get with no token + required=True → AuthRequiredError
    - get with no token + required=False → None
    - store/clear roundtrip
    - Concurrent get() with expired token via asyncio.gather → exactly one refresh call
    - keyring unavailable → CredentialStorageError with actionable message
    - Secret-omission: serialize all log output and assert no tokens in it
```

---

## Integration Points

```yaml
RECORDER:
  Phase 3 does NOT call Recorder directly — Phase 5 tools wrap auth calls and record.
  However: any debug log in oauth.py/credentials.py MUST go through src/events/redaction.py.

CONFIG:
  source: src.config.ApiAuthConfig (already exists)
  consumer: src/auth/oauth.py reads provider/client_id/authorize_url/token_url/scopes

LOGGING:
  destination: stderr only (use logging.getLogger("mcp.auth"))
  level: MCP_LOG_LEVEL env var (default INFO)
  redaction: src/events/redaction.py.redact_body for any token-bearing payload

ENV VARS INTRODUCED:
  - OAUTH_CALLBACK_PORT (default 8765)
  - MCP_CREDENTIALS_STORAGE (default "keyring"; "file" reserved for future)

DEPENDENCIES (already present in requirements.txt — DO NOT add):
  - httpx>=0.27.0  (line 8 of requirements.txt)
  - keyring>=24.0.0  (line 11 of requirements.txt)
  Phase 3 introduces NO new dependencies.
```

---

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

Zero errors. `# type: ignore` only on third-party imports lacking stubs (one-line comment why).

### Level 2 — Unit tests

```bash
pytest tests/auth/ -v       # focused
pytest tests/ -v            # full (must include 27 existing events tests)
```

Required test categories:
- Happy path (OAuth flow → token stored → get returns token)
- PKCE correctness (verifier/challenge math)
- Concurrent refresh race (asyncio.gather → exactly one refresh)
- Keyring unavailable → CredentialStorageError
- Callback bind address is 127.0.0.1 (not 0.0.0.0)
- Callback server closed after exchange
- Secret-omission: dump log capture, grep for token/refresh_token/client_secret → 0 matches

### Level 3 — Smoke test

```bash
# Module imports cleanly and exports the public API
python -c "from src.auth import OAuth, Credentials, TokenInfo, AuthRequiredError, CredentialStorageError; print('ok')"
```

---

## MCP Security Checklist

- [ ] **No secrets in logs.**
      `grep -riE 'authorization:|bearer |client_secret|access_token|refresh_token' src/auth/`
      → only matches inside string constants used for redaction or as dict keys.
- [ ] **All HTTP traffic logging routes through `src/events/redaction.py`.**
      `grep -rn 'redact_headers\|redact_body\|redact_url' src/auth/`
      → at least one match per file that emits HTTP logs.
- [ ] **Credentials read/written ONLY via `keyring`** —
      `grep -rn 'open(' src/auth/` excluding tests → zero matches writing creds to disk.
- [ ] **OAuth callback server binds to `127.0.0.1` only.**
      `grep -n 'localhost\|127.0.0.1\|0.0.0.0' src/auth/oauth.py`
      → only `127.0.0.1` matches. `localhost` and `0.0.0.0` must NOT appear
      (browsers can treat localhost vs 127.0.0.1 as different OAuth origins).
- [ ] **Callback server lifetime is bounded** —
      `grep -n 'server.close\|finally' src/auth/oauth.py`
      → close call inside a finally / context manager.
- [ ] **Pydantic validates every external input** — TokenInfo / OAuthConfig are
      Pydantic models; raw dicts from token endpoint go through `TokenInfo(**data)`.
- [ ] **Error messages do not leak internals** — no env var values, file paths, or
      stack traces in `CredentialStorageError` / `AuthError` messages exposed upward.
- [ ] **No new pip dependencies** — both `httpx` and `keyring` are already present in
      `requirements.txt`. Confirm by inspecting the file; do NOT modify it.
- [ ] **Stdout has zero output** from `src/auth/` — all logging via stderr logger.

---

## Risks

1. **Concurrent token refresh race** — two tools call `Credentials.get(api_id)` with
   an expired token simultaneously. Without a lock, both refresh; the second refresh
   may use a refresh_token already invalidated by the first.
   *Recovery:* `dict[str, asyncio.Lock]` in `Credentials`, lazily created per `api_id`.
   Acquire before expiry check; release in `finally`. Test with `asyncio.gather(get, get)`
   and assert exactly one HTTP call to the token endpoint.

2. **Keyring unavailable on headless Linux** — `keyring.get_password()` raises
   `NoKeyringError` *lazily* (not on import). If we wrap only the import, the error
   leaks out of `get()` as a cryptic third-party exception.
   *Recovery:* Wrap the *first* keyring call in a try/except; cache "unavailable" state
   so we raise `CredentialStorageError` immediately on subsequent calls without retrying.

3. **Callback server bound to 0.0.0.0 by accident** — `asyncio.start_server(host=None)`
   defaults to all interfaces. A typo or copy-paste from a docs example exposes the
   callback to the network during the OAuth flow.
   *Recovery:* Always pass `host="127.0.0.1"` explicitly. Test asserts the bound socket's
   `getsockname()[0] == "127.0.0.1"`. Code review checklist item.

---

## Final Checklist

- [ ] `ruff check src/ tests/ --fix` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all green (incl. 27 existing events tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] `python -c "from src.auth import OAuth, Credentials, TokenInfo"` succeeds
- [ ] No new pip dependencies — `httpx` and `keyring` already in `requirements.txt`
- [ ] Acceptance criteria from "Success Criteria" — all checked
