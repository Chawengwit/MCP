# PRP — Phase 4: API Gateway (`src/gateway/`)

## Goal

Implement a generic async HTTP client (`src/gateway/api_client.py`) supporting REST and
GraphQL, plus response handlers (`src/gateway/handlers.py`) that normalize all responses
into the project's standard `{data, metadata}` / `{error}` shape. The gateway delegates
authentication to `src/auth/Credentials` (Phase 3) and prepares responses for Phase 5
tools.

## Why

- This is **Phase 4** of [docs/plan.md](../docs/plan.md).
- Phase 5 tools (`fetch_data`, `send_data`, `execute_graphql`) call the gateway — without
  it, no tool can reach a real API.
- Centralizing error normalization, redaction, and size enforcement in one module keeps
  every tool's behavior consistent and security-correct.

## What

### `src/gateway/api_client.py` — REST + GraphQL clients

- `RestClient`:
  - async `request(method, url, *, params=None, json=None, headers=None) -> httpx.Response`
  - method ∈ {GET, POST, PUT, DELETE, PATCH}
  - **Auth resolution lives in Phase 5 tools** (via `resolve_auth_headers()`); Phase 5
    passes pre-computed auth headers per request via the `headers=` kwarg. The gateway
    itself stays auth-agnostic.
  - For tests / advanced users, the constructor accepts an optional
    `auth_provider: Callable[[], Awaitable[dict[str, str]]] | None = None` — when set,
    its result is merged into every request's headers (lowest precedence; per-request
    `headers` always wins). Default `None` means "no auto-injection".
  - default 30s timeout, configurable via `MCP_REQUEST_TIMEOUT_SEC` or per-API
  - retry: exponential backoff for HTTP 429 + 5xx, max 3 retries
- `GraphQLClient`:
  - async `execute(url, query, *, variables=None, operation_name=None, headers=None)
    -> httpx.Response`
  - POST with JSON body `{query, variables, operationName}`
  - same auth + timeout + retry behavior as RestClient

### `src/gateway/handlers.py` — Response normalization

- `normalize_rest_response(*, api_id, endpoint, response, started_at) -> dict`
  - HTTP 2xx → `{data, metadata}` shape from CLAUDE.md § Response Format
  - 4xx/5xx → `{error: {code, message, details}}` with mapped `code`
  - Status code mapping:
    - 401/403 → `AUTH_REQUIRED` (or `AUTH_FAILED` after refresh attempt)
    - 404 → `ENDPOINT_NOT_FOUND`
    - 422 → `VALIDATION_ERROR`
    - 429 → `RATE_LIMITED` (extract `Retry-After` into `details.retry_after`)
    - 5xx → `UPSTREAM_ERROR`
  - Extract rate-limit headers (`X-RateLimit-*`) → `metadata.rate_limit_remaining`,
    `metadata.rate_limit_reset` when present
- `normalize_graphql_response(*, api_id, response, started_at) -> dict`
  - Always parse body as JSON
  - If body has `errors` → include in result (partial success allowed)
  - If body has `data` and `errors` → return `{data, errors, metadata}` (NOT collapsed
    into error shape)
  - If only `errors` (no data) → return `{error: {code: "UPSTREAM_ERROR", ...}}`
- Both functions enforce response size:
  - `MCP_MAX_RESPONSE_BYTES` (default 10MB)
  - JSON / text → truncate at byte boundary, set `metadata.truncated=true`,
    `metadata.total_bytes`, `metadata.returned_bytes`, optional `metadata.next_cursor`
  - Binary (Content-Type starts with `image/`, `application/octet-stream`, etc.) →
    error `RESPONSE_TOO_LARGE` if over limit
  - Streaming / chunked transfer encoding → error `RESPONSE_TOO_LARGE`
- All logging uses `src/events/redaction.py` (`redact_headers`, `redact_body`,
  `redact_url`) — never reimplemented.
- gzip handling: httpx auto-decompresses; size check applied to decoded bytes.

### `src/gateway/__init__.py`

Export: `RestClient`, `GraphQLClient`, `normalize_rest_response`,
`normalize_graphql_response`, gateway-specific exceptions.

### Success Criteria

- [ ] REST client: GET/POST/PUT/DELETE/PATCH all work via mocked httpx.
- [ ] GraphQL client: query and mutation both work; operation_name passed through.
- [ ] Auth injection: per-request `headers=` always honored; optional `auth_provider`
      (when set) merges underneath as lowest precedence.
- [ ] Retry logic: 429 + 5xx retried up to 3 times with exponential backoff; 4xx (other)
      not retried.
- [ ] Error normalization: each mapped status code produces the documented error code.
- [ ] GraphQL partial success: response with both `data` and `errors` returns both,
      not collapsed into an error.
- [ ] Response truncation: JSON > limit → success + `metadata.truncated=true`; binary
      > limit → `RESPONSE_TOO_LARGE` error.
- [ ] All logging routes through `src/events/redaction.py`.
- [ ] Phase 3 tests still pass; existing 27 events tests still pass.
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest tests/` all green.

---

## All Needed Context

### Documentation & References

```yaml
- doc: CLAUDE.md
  section: "Response Format Conventions"
  critical: Success {data, metadata}; error {error: {code, message, details}}; never bare strings.

- doc: CLAUDE.md
  section: "Standard Error Codes"
  critical: AUTH_REQUIRED, ENDPOINT_NOT_FOUND, RATE_LIMITED, UPSTREAM_ERROR, VALIDATION_ERROR, RESPONSE_TOO_LARGE — exact spelling.

- doc: CLAUDE.md
  section: "Large Responses — Truncation Rule"
  critical: JSON/text truncate (success + metadata.truncated); binary/streaming error with RESPONSE_TOO_LARGE.

- doc: CLAUDE.md
  section: "GraphQL Specifics"
  critical: GraphQL can return both data AND errors. Surface both. Do NOT collapse partial-success into a flat error.

- file: src/events/redaction.py
  why: REUSE for all logging — redact_headers, redact_body, redact_url
  lines: 1–80

- file: src/events/recorder.py
  why: Public-class pattern, async lifecycle
  lines: 22–60

- file: src/auth/credentials.py  (from Phase 3)
  why: Phase 5 tools call Credentials.get() directly and pass headers per-request.
       The gateway's optional auth_provider hook exists for tests/advanced cases only.
  lines: full

- file: src/config.py
  why: ApiConfig.endpoints (EndpointConfig), ApiLimitsConfig (timeout_seconds, max_retries) already defined
  lines: 26–53

- url: https://www.python-httpx.org/async/
  why: httpx.AsyncClient context manager; timeout; raise_for_status semantics
  critical: Use AsyncClient(timeout=httpx.Timeout(...)); do NOT use raise_for_status — we map manually.

- url: https://www.python-httpx.org/advanced/#event-hooks
  why: Optional event hooks for logging request/response — useful to ensure ALL traffic is redacted

- url: https://graphql.org/learn/serving-over-http/
  why: GraphQL POST body shape: {query, variables, operationName}
  critical: GraphQL errors live in body['errors'], NOT in HTTP status
```

### Current Codebase

```
src/
├── auth/                     ← Phase 3 (must be merged before Phase 4)
├── config.py                 ← ApiConfig, ApiLimitsConfig already exist
├── events/                   ← redaction helpers (reuse), Recorder (Phase 5 wires it)
└── gateway/                  ← does NOT yet exist — to be created
```

### Desired Codebase

```
src/gateway/
├── __init__.py               ← export public API
├── api_client.py             ← RestClient, GraphQLClient
└── handlers.py               ← normalize_rest_response, normalize_graphql_response, GatewayError types

tests/gateway/
├── __init__.py
├── conftest.py               ← mock httpx, mock auth_provider
├── test_api_client.py        ← method matrix, retry, auth injection, timeout
└── test_handlers.py          ← status mapping, GraphQL partial success, truncation, redaction reuse
```

### Known Gotchas (Phase 4 specific)

```python
# CRITICAL — GraphQL partial success
# A GraphQL endpoint returns HTTP 200 with body {"data": {...}, "errors": [...]}.
# Naive code: "HTTP 200 → return data". This loses errors → Claude sees fake success.
# Fix: parse body before deciding. If body has BOTH data and errors → return both.
# File: src/gateway/handlers.py — normalize_graphql_response checks body['errors'] always.

# CRITICAL — Truncation safety
# Truncating a JSON byte stream mid-string yields invalid JSON.
# Fix: truncate at byte boundary, return as TEXT in data field with metadata.truncated=true,
#   metadata.total_bytes, metadata.returned_bytes, metadata.hint.
# Do NOT attempt to "repair" the JSON — Claude can re-request with pagination.
# File: src/gateway/handlers.py — _truncate_payload returns bytes + flag; never re-parses.

# CRITICAL — Binary truncation is unsafe
# Cutting an image/PDF mid-stream yields unusable data.
# Fix: detect binary via Content-Type prefix ('image/', 'application/octet-stream',
#   'application/pdf', 'video/', 'audio/'). If oversized → RESPONSE_TOO_LARGE error.
# File: src/gateway/handlers.py — _is_binary_content_type helper.

# CRITICAL — Retry on POST is dangerous
# Retrying a non-idempotent POST may double-create. Standard practice:
#   - GET, HEAD, PUT, DELETE → safe to retry
#   - POST → retry only on 429 / 5xx that occur BEFORE the server processed the request
#     (which we cannot reliably distinguish). Default: still retry on 429/5xx — document
#     this as a known limitation; users with strict idempotency requirements should
#     supply an Idempotency-Key header.
# File: src/gateway/api_client.py — _should_retry includes POST; comment the trade-off.

# CRITICAL — Auth header redaction in logs
# httpx event hooks fire with the real headers. If we log the request via the hook
# without redaction, tokens land in stderr.
# Fix: in the hook, deep-copy headers and run through redact_headers BEFORE logging.
# File: src/gateway/api_client.py — log_request hook uses redact_headers.

# CRITICAL — Timeout is httpx.Timeout, not float
# httpx accepts (connect, read, write, pool) — passing a float means "all four equal".
# Fix: httpx.Timeout(MCP_REQUEST_TIMEOUT_SEC) is fine for our needs.
# File: src/gateway/api_client.py — wrap in httpx.Timeout for type clarity.
```

---

## Implementation Blueprint

### Data Models

```python
# src/gateway/handlers.py
class GatewayError(RuntimeError):
    """Base for gateway-internal errors before they become {error: ...} dicts."""

class ResponseTooLargeError(GatewayError): ...
class UnexpectedContentTypeError(GatewayError): ...

# Standard response dict shapes (NOT Pydantic — match CLAUDE.md exactly)
# Success:  {"data": Any, "metadata": {...}}
# Partial:  {"data": Any, "errors": [...], "metadata": {...}}
# Error:    {"error": {"code": str, "message": str, "details": dict}}
```

### Tasks (in order)

```yaml
Task 1 — Skeleton:
  CREATE src/gateway/__init__.py
  CREATE src/gateway/api_client.py:
    - Stubs for RestClient, GraphQLClient with method signatures
  CREATE src/gateway/handlers.py:
    - GatewayError + subclasses
    - Stubs for normalize_rest_response, normalize_graphql_response

Task 2 — RestClient core:
  COMPLETE src/gateway/api_client.py:
    - __init__(*, base_url, auth_provider=None, timeout_seconds=30, max_retries=3)
    - async request(method, path, *, params, json, headers)
    - Build full URL from base_url + path
    - Header precedence (lowest → highest):
        defaults  →  auth_provider() (if set)  →  per-request headers kwarg
      Per-request headers always win — Phase 5 tools rely on this.
    - httpx.AsyncClient with httpx.Timeout
    - Return httpx.Response (no status check — handlers map)
  KEY DECISION: auth_provider is OPTIONAL (default None). Phase 5's standard flow
    computes headers via resolve_auth_headers() and passes them per request; the
    auth_provider hook exists only for tests and advanced users that prefer
    constructor-time injection.

Task 3 — Retry logic:
  ADD to RestClient:
    - _should_retry(status_code, attempt) -> bool
    - Backoff: 0.5s * 2**attempt + jitter, capped at 8s
    - Retry HTTP 429 (respect Retry-After header), 502, 503, 504
    - Do NOT retry 500 (often a real bug; retrying masks it)
  KEY DECISION: Document POST retry as best-effort; recommend Idempotency-Key.

Task 4 — GraphQLClient:
  ADD to src/gateway/api_client.py:
    - GraphQLClient(*, url, auth_provider=None, timeout_seconds=30, max_retries=3)
      (auth_provider optional, same semantics as RestClient)
    - async execute(query, *, variables, operation_name, headers)
    - POST to self.url with JSON body {query, variables, operationName}
    - Reuse RestClient internally OR share retry/auth code via small helper

Task 5 — REST response normalizer:
  COMPLETE handlers.normalize_rest_response:
    - Parse body: JSON if Content-Type contains 'json'; text otherwise
    - 2xx → {data, metadata: {source, endpoint, timestamp, duration_ms, status_code}}
    - 4xx/5xx → {error: {code, message, details: {status_code, body_excerpt}}}
    - Map status to code per "Standard Error Codes"
    - Extract X-RateLimit-Remaining, X-RateLimit-Reset → metadata
    - Apply size enforcement (truncate JSON/text; RESPONSE_TOO_LARGE for binary)
  MIRROR pattern from: CLAUDE.md § Response Format Conventions exactly.

Task 6 — GraphQL response normalizer:
  COMPLETE handlers.normalize_graphql_response:
    - Body must parse as JSON; if not → UPSTREAM_ERROR
    - data + errors → return {data, errors, metadata}
    - errors only → return {error: {code: UPSTREAM_ERROR, ..., details: {graphql_errors}}}
    - data only → return {data, metadata}
    - Apply size enforcement to data field

Task 7 — Logging hooks (with redaction):
  ADD to api_client.py:
    - log_request hook: redact_headers + redact_url before logging
    - log_response hook: redact_headers + redact_body (size-bounded preview)
  KEY DECISION: Hooks attached only when MCP_LOG_DEBUG_ENABLED=true.

Task 8 — Tests:
  CREATE tests/gateway/conftest.py:
    - mock_httpx fixture (respx or httpx.MockTransport)
    - mock_auth_provider fixture returning {"Authorization": "Bearer test"}
      (used to verify the optional auth_provider hook precedence vs per-request headers)
  CREATE tests/gateway/test_api_client.py:
    - GET, POST, PUT, DELETE, PATCH each succeed
    - Timeout configurable
    - Retry: 429 then 200 → succeeds on second try
    - Retry: 5xx three times → final failure surfaced (not retried indefinitely)
    - 4xx (non-429) → no retry
    - Per-request headers kwarg appears verbatim in outbound request
    - Optional auth_provider merges in when no per-request header overrides it
    - Per-request header overrides auth_provider value for the same key
    - GraphQL POST body shape correct
  CREATE tests/gateway/test_handlers.py:
    - Status map: 401→AUTH_REQUIRED, 404→ENDPOINT_NOT_FOUND, 422→VALIDATION_ERROR,
      429→RATE_LIMITED (with retry_after), 503→UPSTREAM_ERROR
    - GraphQL partial success: body {data, errors} → result has both
    - GraphQL errors-only → error shape with UPSTREAM_ERROR
    - Truncation: JSON > limit → success + metadata.truncated=true + sizes
    - Truncation: binary > limit → RESPONSE_TOO_LARGE error
    - Rate-limit header extraction → metadata.rate_limit_*
    - Redaction reuse: capture log output, assert no Authorization header value
```

---

## Integration Points

```yaml
RECORDER:
  Phase 4 does NOT call Recorder directly. Phase 5 tools call gateway, then call
  recorder.record_audit/usage/insight. Gateway debug logging (MCP_LOG_DEBUG_ENABLED)
  may call recorder.record_debug — wire that in Phase 5.

CONFIG:
  source: src.config.ApiConfig.limits.timeout_seconds, .max_retries
  consumer: RestClient/GraphQLClient constructor takes these as kwargs
  source: src.config.ApiConfig.endpoints (EndpointConfig)
  consumer: tools (Phase 5) — gateway accepts pre-resolved URL/method

LOGGING:
  destination: stderr only via logging.getLogger("mcp.gateway")
  redaction: src/events/redaction.py — every request/response log path
  level: MCP_LOG_LEVEL (default INFO); hooks attach only when MCP_LOG_DEBUG_ENABLED=true

ENV VARS INTRODUCED:
  - MCP_MAX_RESPONSE_BYTES (default 10485760 = 10 MiB)
  - MCP_REQUEST_TIMEOUT_SEC (default 30)

DEPENDENCIES (already present in requirements.txt — DO NOT add):
  - httpx>=0.27.0  (line 8 of requirements.txt)
  Phase 4 introduces NO new dependencies.
```

---

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

### Level 2 — Unit tests

```bash
pytest tests/gateway/ -v
pytest tests/ -v            # full suite
```

Required test categories:
- Happy path: each method + GraphQL execute
- Retry: 429 with Retry-After header, 5xx triggers, 4xx does not
- Status mapping for every code in CLAUDE.md
- GraphQL partial success
- Truncation: JSON over limit, binary over limit
- Redaction in logs (capture stderr; grep for Bearer/Authorization values)

### Level 3 — Integration smoke

```bash
python -c "from src.gateway import RestClient, GraphQLClient, normalize_rest_response, normalize_graphql_response; print('ok')"
```

---

## MCP Security Checklist

- [ ] **No secrets in logs.**
      `grep -riE 'authorization:|bearer |client_secret|access_token' src/gateway/`
      → only matches inside redaction-related constants.
- [ ] **All HTTP traffic logging routes through `src/events/redaction.py`.**
      `grep -rn 'redact_headers\|redact_body\|redact_url' src/gateway/`
      → at least one match per file emitting HTTP logs.
- [ ] **Pydantic / explicit shape validates external inputs** — body parsed via
      `response.json()` then handled defensively (no assumptions about keys).
- [ ] **Error messages do not leak internals** — error.details may include
      `status_code` and `body_excerpt` (truncated, redacted), but NOT full URLs
      with query strings or env var names.
- [ ] **No new pip dependencies** — `httpx` is already in `requirements.txt`.
      Confirm by inspecting the file; do NOT modify it.
- [ ] **Stdout has zero output** from `src/gateway/` — all logs via stderr logger.
- [ ] **Truncation rule honored** — JSON/text success-with-truncation; binary error.
- [ ] **GraphQL partial success preserved** — both data and errors in response.

---

## Risks

1. **GraphQL partial success collapsed to error** — naive `if response.errors: raise`
   loses the `data` field that Claude could still use.
   *Recovery:* `normalize_graphql_response` always inspects body; only returns
   `{error: ...}` when there is no `data`. Tests with mock body
   `{"data": {"user": {...}}, "errors": [{...}]}` assert result has both keys.

2. **Auth header logged via httpx event hook** — attaching a vanilla `log_request` hook
   that prints `request.headers` exposes the Bearer token.
   *Recovery:* The hook must call `redact_headers(dict(request.headers))` before any
   logging. Test captures stderr while making a request with a fake Bearer token and
   asserts the literal token string never appears.

3. **Retry storm on flapping upstream** — if every retry attempt also fails with 5xx,
   exponential backoff still amounts to ~14 seconds of blocking before surfacing the
   error to Claude, and may amplify load on the upstream.
   *Recovery:* Cap backoff at 8s; cap total retries at 3; respect `Retry-After` for 429.
   Document this in the README troubleshooting section. Tests assert max 3 attempts.

---

## Final Checklist

- [ ] `ruff check src/ tests/ --fix` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all green (incl. Phase 3 + 27 events tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] `python -c "from src.gateway import RestClient, GraphQLClient"` succeeds
- [ ] No new pip dependencies — `httpx` already in `requirements.txt`
- [ ] Acceptance criteria from "Success Criteria" — all checked
