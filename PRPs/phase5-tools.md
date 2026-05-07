# PRP — Phase 5: Tools & Integration (`src/tools/`)

## Goal

Implement the user-facing MCP tools — `fetch_data`, `send_data`, `execute_graphql`, and
`get_status` — wiring `Credentials` (Phase 3) for auto-authentication, `RestClient` /
`GraphQLClient` (Phase 4) for HTTP, and `Recorder` (Phase 7, complete) for activity
logging. `list_apis` already exists from Phase 2 — extend it only if needed for
consistency. Each tool returns the standard response shape from `CLAUDE.md`, and every
invocation produces `audit + usage + insight` events.

## Why

- This is **Phase 5** of [docs/plan.md](../docs/plan.md).
- Phases 3 and 4 are infrastructure; Phase 5 is what Claude actually invokes.
- This is where `Recorder` integration finally becomes mandatory — the gateway and auth
  modules logged through redaction helpers, but tools are the contract surface for
  observability.

## What

### `src/tools/mcp_tools.py` — Tool implementations

Each tool is an `async def` taking a Pydantic input model and returning a `dict` matching
the standard response shape.

- `fetch_data(input: FetchDataInput, *, context: ToolContext) -> dict`
  - GET against `api_id`/`endpoint` resolved via `ApiConfig.endpoints`
  - `filters` → query parameters
- `send_data(input: SendDataInput, *, context: ToolContext) -> dict`
  - POST or PUT (per `EndpointConfig.method`) with `payload` body
- `execute_graphql(input: GraphQLInput, *, context: ToolContext) -> dict`
  - Resolves `api_id` (must be GraphQL type) → calls `GraphQLClient.execute`
  - Variables passed through; `operation_name` optional
- `get_status(input: GetStatusInput, *, context: ToolContext) -> dict`
  - For each configured API: report `auth_state` ∈ {`authenticated`, `expired`,
    `unauthenticated`, `not_required`} without triggering an OAuth flow.
  - Uses `Credentials.peek(api_id)` (added in Phase 3) — strictly read-only.

### `ToolContext` — Dependency container

```python
@dataclass
class ToolContext:
    configs: dict[str, ApiConfig]
    credentials: Credentials
    rest_client_factory: Callable[[ApiConfig], RestClient]
    graphql_client_factory: Callable[[ApiConfig], GraphQLClient]
    recorder: Recorder
```

This keeps tools test-friendly — pass a `ToolContext` with mocks; no global state.

### Per-tool flow (every tool follows this exactly)

```
1. Generate session_id (uuid4) + start time
2. Validate input via Pydantic (already done by signature)
3. Resolve ApiConfig from context.configs[api_id]
   - Missing → return error API_NOT_CONFIGURED
4. Resolve EndpointConfig from config.endpoints[endpoint]
   - Missing → return error ENDPOINT_NOT_FOUND
5. Resolve auth headers based on config.auth.type:
   - None (no auth block) → headers = {}
   - "oauth2"  → token = await context.credentials.get(api_id)
                 headers = {"Authorization": f"Bearer {token.access_token}"}
                 Catch AuthRequiredError → return AUTH_REQUIRED error
   - "bearer"  → token = os.environ.get(config.auth.token_env)
                 if not token → return AUTH_REQUIRED error
                 headers = {"Authorization": f"Bearer {token}"}
   - "api_key" → key = os.environ.get(config.auth.key_env)
                 if not key → return AUTH_REQUIRED error
                 headers = {config.auth.header_name: key}
   - other     → return error VALIDATION_ERROR (unknown auth.type)
6. Call gateway:
   - REST: rest_client_factory(config).request(method, path, params, json, headers)
   - GraphQL: graphql_client_factory(config).execute(url, query, variables, headers)
7. Normalize via handlers.normalize_*_response
8. In a `finally` block:
   - record_audit(session_id, tool, result, duration_ms)
   - record_usage(tool, status, duration_ms)
   - record_insight(session_id, tool, tool_args=<redacted input>)
9. Return the normalized response dict
```

### Auth resolution helper

```python
# src/tools/auth_resolver.py (new module, small)
async def resolve_auth_headers(
    config: ApiConfig, api_id: str, credentials: Credentials
) -> dict[str, str]:
    """Returns headers dict or raises AuthRequiredError.

    Branches on config.auth.type. Only 'oauth2' uses Credentials; 'bearer'/'api_key'
    read directly from environment.
    """
```

### Server wiring (`src/server.py`)

- At startup: construct `Recorder.from_env()`, `Credentials(...)`, factories for
  RestClient/GraphQLClient. Build `ToolContext`. Register tools with the existing
  registry from Phase 2.
- At shutdown: `await recorder.stop()` (drain queue) before exit.
- SIGINT/SIGTERM handler: cancel running tasks → drain → exit.

### Input models

```python
class FetchDataInput(BaseModel):
    api_id: str
    endpoint: str
    filters: dict[str, Any] | None = None

class SendDataInput(BaseModel):
    api_id: str
    endpoint: str
    payload: dict[str, Any]

class GraphQLInput(BaseModel):
    api_id: str
    query: str
    variables: dict[str, Any] | None = None
    operation_name: str | None = None

class GetStatusInput(BaseModel):
    api_id: str | None = None      # None → all configured APIs
```

### Success Criteria

- [ ] `fetch_data`, `send_data`, `execute_graphql` each succeed end-to-end against
      mocked gateway + mocked auth.
- [ ] On `AuthRequiredError` from credentials, tool returns standard
      `{error: {code: "AUTH_REQUIRED", ...}}` — no exception leaks.
- [ ] Every successful tool call writes one `audit` + one `usage` + one `insight` event.
- [ ] Every failed tool call still writes `audit` + `usage` (`status="error"`) + `insight`.
- [ ] `list_apis` response (existing) still omits all auth fields.
- [ ] `get_status` does NOT trigger an OAuth flow (read-only check).
- [ ] Phase 3 + Phase 4 tests still pass; existing 27 events tests still pass.
- [ ] Server starts cleanly; `await recorder.stop()` runs on shutdown without error.
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest tests/` all green.

---

## All Needed Context

### Documentation & References

```yaml
- doc: CLAUDE.md
  section: "Response Format Conventions"
  critical: Standard {data, metadata} or {error: {code, message, details}} shape — do not deviate.

- doc: CLAUDE.md
  section: "Activity Logging > Recording Rules"
  critical: All record_* are async, non-blocking, NEVER raise. Tools do audit + usage + insight every call.

- doc: CLAUDE.md
  section: "Standard Error Codes"
  critical: API_NOT_CONFIGURED, ENDPOINT_NOT_FOUND, AUTH_REQUIRED, VALIDATION_ERROR — exact strings.

- file: src/events/recorder.py
  why: Record method signatures (record_audit, record_usage, record_insight)
  lines: full

- file: src/events/redaction.py
  why: Redact tool_args before record_insight (filters/payload may contain secrets)
  lines: 1–80

- file: src/auth/credentials.py  (Phase 3)
  why: Credentials.get(api_id) returns TokenInfo or raises AuthRequiredError
  lines: full

- file: src/gateway/api_client.py  (Phase 4)
  why: RestClient/GraphQLClient signatures
  lines: full

- file: src/gateway/handlers.py  (Phase 4)
  why: normalize_rest_response, normalize_graphql_response
  lines: full

- file: src/tools/registry.py
  why: Existing tool registration pattern from Phase 2
  lines: full

- file: src/tools/builtin.py
  why: Pattern for an existing tool (list_apis) — follow shape conventions
  lines: full

- file: src/server.py
  why: Existing startup/shutdown — extend with Recorder + tool context wiring
  lines: full
```

### Current Codebase

```
src/
├── auth/                     ← Phase 3 (must be merged)
├── gateway/                  ← Phase 4 (must be merged)
├── events/                   ← Recorder, redaction
├── tools/
│   ├── builtin.py            ← list_apis (Phase 2) — already implemented
│   ├── registry.py           ← tool registry
│   └── spec.py               ← ToolSpec definition
└── server.py                 ← extend for Recorder + ToolContext
```

### Desired Codebase

```
src/tools/
├── mcp_tools.py              ← NEW: fetch_data, send_data, execute_graphql, get_status
├── context.py                ← NEW: ToolContext dataclass + factory helpers
├── builtin.py                ← MODIFY only if list_apis needs auth-field stripping fix
├── registry.py               ← unchanged
└── spec.py                   ← unchanged

src/server.py                 ← MODIFY: build ToolContext at startup, drain Recorder at shutdown

tests/tools/
├── test_mcp_tools.py         ← per-tool happy/error paths + Recorder assertions
├── test_get_status.py        ← (or merge into above) — explicit no-OAuth-trigger test
└── test_integration.py       ← end-to-end through ToolContext with mocks
```

### Known Gotchas (Phase 5 specific)

```python
# CRITICAL — Recorder calls must run even on exception
# If a tool raises (or returns early), record_audit/usage/insight may be skipped.
# Fix: wrap the entire tool body in try/finally; compute status from result vs caught exc.
# File: src/tools/mcp_tools.py — every tool has try/finally with Recorder calls in finally.

# CRITICAL — record_insight may leak secrets via tool_args
# tool_args = input.model_dump() includes 'payload' which can contain api_key, password etc.
# Fix: pass tool_args through redact_body before record_insight.
# File: src/tools/mcp_tools.py — redacted_args = redact_body(input.model_dump())

# CRITICAL — get_status must NOT trigger OAuth
# Calling Credentials.get(api_id) refreshes near-expiry tokens; even with
# required=False, it could issue a refresh request. get_status must be side-effect-free.
# Fix: use Credentials.peek(api_id) — added in Phase 3 — which returns the stored
#   TokenInfo or None without any network call or refresh attempt.
# File: src/tools/mcp_tools.py — get_status uses peek(), never get().

# CRITICAL — AuthRequiredError must surface as AUTH_REQUIRED, not crash
# When the auth_provider closure raises inside RestClient.request, httpx wraps it
# into an exception group depending on Python version.
# Fix: tool catches AuthRequiredError ABOVE the gateway call (call credentials.get
#   explicitly first), or RestClient catches and re-raises cleanly. Simplest:
#   tool calls auth_provider() directly once, then passes static headers to gateway.
# File: src/tools/mcp_tools.py — fetch token before constructing client.

# CRITICAL — Server shutdown order
# If Recorder.stop() is awaited AFTER httpx clients are closed, no problem.
# If stop() never runs (e.g. SIGKILL), queued events lost — accepted.
# Fix: register signal handler that cancels tool tasks → awaits recorder.stop() →
#   exits. Use try/except NotImplementedError for Windows where add_signal_handler fails.
# File: src/server.py — extend existing Phase 2 shutdown logic.

# CRITICAL — Pydantic input validation errors
# Pydantic raises ValidationError; the MCP wrapper must convert to standard error shape.
# Fix: tool wrapper around the actual handler catches ValidationError → VALIDATION_ERROR
#   with details: {field_errors}.
# File: src/tools/mcp_tools.py — _wrap_tool helper or per-tool try/except.

# CRITICAL — ToolSpec.input_schema is JSONSchema dict, NOT Pydantic model
# Phase 2's ToolSpec defines input_schema as dict[str, Any] (JSONSchema).
# Registering a Pydantic class directly will fail or produce wrong schemas at runtime.
# Fix: at registration time, call FetchDataInput.model_json_schema() (Pydantic v2 API)
#   and pass the resulting dict to ToolSpec(input_schema=...).
# At handler-invocation time, parse arguments dict via FetchDataInput(**arguments) to
# get a validated model instance.
# File: src/server.py registration code (in extended _build_registry — see Task 8 below
#   for the concrete code skeleton).
```

---

## Implementation Blueprint

### Tasks (in order)

```yaml
Task 1 — ToolContext + skeletons:
  CREATE src/tools/context.py:
    - ToolContext dataclass with all dependencies
    - build_default_context() factory reading env + config
  CREATE src/tools/mcp_tools.py:
    - Pydantic input models
    - Async stubs for each tool returning {"error": {"code": "NOT_IMPLEMENTED", ...}}

Task 2 — Auth resolver helper:
  CREATE src/tools/auth_resolver.py:
    - async resolve_auth_headers(config, api_id, credentials) -> dict[str, str]
    - Branch on config.auth (None / "oauth2" / "bearer" / "api_key")
    - Raise AuthRequiredError when token missing for bearer/api_key
    - For oauth2, propagate AuthRequiredError from credentials.get()
  KEY DECISION: Centralized helper keeps each tool's flow identical and testable.

Task 3 — fetch_data:
  COMPLETE fetch_data in src/tools/mcp_tools.py:
    - Resolve api_id → ApiConfig (else API_NOT_CONFIGURED)
    - Resolve endpoint → EndpointConfig (else ENDPOINT_NOT_FOUND)
    - headers = await resolve_auth_headers(config, api_id, credentials)
      Catch AuthRequiredError → return AUTH_REQUIRED error shape
    - Call rest_client_factory(config).request(method=GET, path=endpoint.path,
        params=filters, headers=headers)
    - Pass response to normalize_rest_response
    - try/finally with Recorder triple
  KEY DECISION: Auth resolution via helper, not closure — keeps error mapping simple
    and centralizes the auth.type branch matrix.

Task 4 — send_data:
  COMPLETE send_data:
    - Same auth/resolve flow as fetch_data via resolve_auth_headers()
    - method = endpoint.method (POST/PUT)
    - body = input.payload (validated dict)

Task 5 — execute_graphql:
  COMPLETE execute_graphql:
    - api_id must resolve to ApiConfig with type=='graphql' (else VALIDATION_ERROR)
    - headers = await resolve_auth_headers(config, api_id, credentials)
    - graphql_client_factory(config).execute(query, variables, operation_name, headers)
    - normalize_graphql_response

Task 6 — get_status:
  COMPLETE get_status:
    - Iterate config.apis (or single api_id)
    - For each: branch on config.auth:
      - config.auth is None → "not_required"
      - config.auth.type == "bearer" → "authenticated" if os.environ.get(token_env)
        else "unauthenticated" (no Credentials involvement)
      - config.auth.type == "oauth2" → credentials.peek(api_id) → TokenInfo | None
        - None → "unauthenticated"
        - expires_at - now < 0 → "expired"
        - else "authenticated"
    - Return {data: [{api_id, type, auth_state, expires_at?}], metadata: {...}}
  KEY DECISION: peek() added in Phase 3 — strictly read-only, no network, no refresh.

Task 7 — Recorder wiring helper:
  ADD to src/tools/mcp_tools.py:
    - _record_invocation(recorder, session_id, tool, result, duration_ms, args)
      Calls record_audit, record_usage, record_insight with redacted args
  KEY DECISION: tool_args redacted via src/events/redaction.redact_body before record_insight.

Task 8 — Server wiring (extend existing _build_registry):
  MODIFY src/server.py:
    - The existing _build_registry(recorder, api_configs) at src/server.py:21–42
      already shows the closure-injection pattern for list_apis. Extend it to also
      inject the new tools with a ToolContext bound via closures.
    - In main() at src/server.py:87:
        configs = load_api_configs(...)        # already done
        recorder = Recorder.from_env()         # already done
        credentials = Credentials(...)         # NEW
        ctx = ToolContext(
            configs=configs, credentials=credentials,
            rest_client_factory=lambda c: RestClient(...),
            graphql_client_factory=lambda c: GraphQLClient(...),
            recorder=recorder,
        )
        registry = _build_registry(ctx)        # signature changes: now takes ctx
    - For each new tool, register a ToolSpec where:
        - input_schema = FetchDataInput.model_json_schema()  (Pydantic v2 → JSONSchema)
        - handler = closure that parses arguments via Pydantic, calls the tool,
          returns the dict result
    - Shutdown: existing try/finally already awaits recorder.stop() (src/server.py:144).
      No new shutdown logic needed — Phase 5 inherits Phase 2's pattern.
  MIRROR pattern from: src/server.py:21–42 (existing _build_registry with list_apis).

Task 9 — Tests:
  CREATE tests/tools/test_mcp_tools.py:
    - fetch_data happy path: returns {data, metadata}; asserts Recorder called 3x
    - fetch_data with unknown api_id → API_NOT_CONFIGURED
    - fetch_data with unknown endpoint → ENDPOINT_NOT_FOUND
    - fetch_data with AuthRequiredError → AUTH_REQUIRED (no exception)
    - send_data happy path
    - execute_graphql happy path; partial-success preserved
    - get_status reflects auth state without invoking OAuth (peek not refresh)
    - Pydantic ValidationError on bad input → VALIDATION_ERROR
    - Recorder still called when tool raises (try/finally)
    - tool_args in record_insight are redacted (no api_key/password values)
  CREATE tests/tools/test_integration.py:
    - End-to-end via ToolContext with mock httpx, mock keyring, real Recorder
      (writes to tmp_path)
    - Inspect generated JSONL files: 1 audit + 1 usage + 1 insight per call
    - grep events for token/secret/auth values → 0 matches
```

---

## Integration Points

```yaml
RECORDER:
  source: src.events.Recorder
  lifecycle: started in src/server.py at boot; stopped at shutdown
  per-tool: try/finally calls record_audit + record_usage + record_insight
  redaction: tool_args run through src/events/redaction.redact_body before record_insight

CREDENTIALS:
  source: src.auth.Credentials (Phase 3)
  used by: each tool calls credentials.get(api_id) once at entry
  read-only access: get_status uses credentials.peek(api_id) — does NOT refresh

GATEWAY:
  source: src.gateway.RestClient, GraphQLClient (Phase 4)
  factories: built once at startup, captured in ToolContext

CONFIG:
  source: src.config.load_api_configs()
  loaded at startup; passed via ToolContext
  hot reload: NOT supported (out of scope)

LOGGING:
  destination: stderr; logger names "mcp.tools.<tool_name>"
  redaction: any debug log goes through src/events/redaction.py

ENV VARS:
  No new env vars in Phase 5 — reuses Phase 3/4 vars.
```

---

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

### Level 2 — Unit + integration tests

```bash
pytest tests/tools/ -v
pytest tests/ -v            # full suite (Phase 3, 4, 7 + tools)
```

Required test categories:
- Happy path for each tool
- Each error code path (API_NOT_CONFIGURED, ENDPOINT_NOT_FOUND, AUTH_REQUIRED,
  VALIDATION_ERROR, plus gateway-mapped codes pass through)
- Recorder triple-write per call (success and failure)
- Recorder on exception via try/finally
- Redaction of tool_args before record_insight
- get_status does NOT trigger OAuth (assert oauth.start_flow not called)
- Server startup/shutdown: Recorder.start() + Recorder.stop() awaited

### Level 3 — Smoke test

```bash
# Server starts and exits cleanly with no stdout output
echo "" | timeout 2 python -m src.server 2>/tmp/mcp_stderr.log; rc=$?
test -z "$(echo '' | timeout 2 python -m src.server 2>/dev/null)"  # stdout empty
test "$rc" -eq 124 -o "$rc" -eq 0   # 124 = timeout SIGTERM, 0 = clean exit
```

---

## MCP Security Checklist

- [ ] **No secrets in logs.**
      `grep -riE 'authorization:|bearer |client_secret|access_token|api_key' src/tools/`
      → only matches inside redaction-related references.
- [ ] **All HTTP traffic logging routes through `src/events/redaction.py`.**
      `grep -rn 'redact_headers\|redact_body\|redact_url' src/tools/`
      → at least the `record_insight` path uses `redact_body`.
- [ ] **Tool responses contain zero auth fields.**
      Run `pytest tests/tools/test_mcp_tools.py::test_list_apis_strips_auth` (or extend
      existing list_apis test) — assert no `auth`, `client_id`, `token`, `secret` keys.
- [ ] **Pydantic validates every tool input.** Each tool signature accepts a Pydantic
      model; raw `dict[str, Any]` never reaches business logic without conversion.
- [ ] **Error messages do not leak internals.** No file paths, env var names,
      stack traces in user-facing error responses; details may include status_code +
      sanitized field_errors only.
- [ ] **Recorder is called on every code path** including exceptions —
      `grep -n 'finally' src/tools/mcp_tools.py` shows finally blocks with record_*.
- [ ] **`get_status` does not trigger OAuth** — test asserts `oauth.start_flow` not
      called when invoking get_status against an expired token.
- [ ] **Stdout has zero output** from `src/tools/` — all logs via stderr logger.
- [ ] **No new pip dependency** — Phase 5 should add no new packages.

---

## Risks

1. **Recorder calls skipped on exception** — a `return` early or unhandled raise
   short-circuits the audit/usage/insight triple, leaving an unrecorded tool call.
   *Recovery:* Every tool body in `try/finally`; the `finally` runs Recorder calls
   regardless. Test triggers a deliberate exception inside the tool and asserts all
   three recorder methods were called.

2. **`record_insight` leaks secrets via `tool_args`** — `payload` for `send_data`
   could contain a password or API key the user is forwarding. Logging it raw in
   `insight` events defeats the entire redaction layer.
   *Recovery:* Always pass `input.model_dump()` through `redact_body` before the
   `record_insight` call. Test uses an input with `payload={"api_key": "secret"}` and
   asserts the insight log line shows `<redacted>` for that field.

3. **`get_status` accidentally triggers OAuth** — a refactor that has `get_status`
   call `credentials.get()` (refreshing) instead of `credentials.peek()` will pop a
   browser window when the user just wanted to see status.
   *Recovery:* Add `Credentials.peek(api_id) -> TokenInfo | None` as a read-only API.
   Test mocks `oauth.start_flow` and asserts it is never called during `get_status`,
   even when the stored token is expired.

---

## Final Checklist

- [ ] `ruff check src/ tests/ --fix` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all green (incl. Phase 3 + 4 + 27 events tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] Server starts and stops cleanly; Recorder drains on shutdown
- [ ] No new pip dependencies added
- [ ] `get_status` does not trigger OAuth (test asserts)
- [ ] Acceptance criteria from "Success Criteria" — all checked
