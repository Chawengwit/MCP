# PRP — Phase 8: HTTP Transport (`src/transport/`)

## Goal

Add an HTTP transport (MCP **Streamable HTTP** spec) to the MCP Data Gateway so the
server can be reached by remote clients (ChatGPT Connectors, MCP Inspector, custom
HTTP clients) **alongside** the existing stdio transport. Selectable via env var;
stdio remains the default. All 247 existing tests must continue to pass without
modification.

## Why

- This is **Phase 8** of [docs/plan.md](../docs/plan.md). Closes the "remote client"
  gap that stdio cannot serve.
- Unlocks ChatGPT Custom Connector usage (the immediate user need), MCP Inspector
  for ergonomic debugging, and any future HTTP-based MCP client.
- Establishes a transport abstraction (`src/transport/`) that future transports
  (WebSocket, etc.) can plug into without re-touching `src/server.py`.

## What

### Selection
- New env var **`MCP_TRANSPORT`** ∈ `{stdio, http}`. Default `stdio` → existing
  behavior unchanged. Any other value → fail-loud `ValueError` at startup.

### HTTP transport behavior
- Boots an **ASGI app** (Starlette) with a single MCP endpoint (`POST/GET/DELETE /mcp`)
  served by `mcp.server.streamable_http_manager.StreamableHTTPSessionManager`.
- Listens on `MCP_HTTP_HOST` (default `127.0.0.1`) port `MCP_HTTP_PORT`
  (default `8080`), served by `uvicorn`.
- **Bearer-token auth middleware**: every request must carry
  `Authorization: Bearer <MCP_HTTP_BEARER_TOKEN>` (compared with
  `secrets.compare_digest`). Missing/invalid → `401`.
- **Loopback safety guard** (fail-loud): if `MCP_HTTP_HOST` is **not** in
  `{127.0.0.1, ::1, localhost}` AND `MCP_HTTP_BEARER_TOKEN` is unset/empty, server
  refuses to start with a clear stderr message and `sys.exit(1)`. Loopback bind +
  no token is allowed (dev). Public bind + no token is rejected.
- All 5 tools (`list_apis`, `fetch_data`, `send_data`, `execute_graphql`,
  `get_status`) work identically over HTTP — same `ToolContext`, same `Recorder`,
  same response shape from CLAUDE.md.

### Out of scope (rejected explicitly)
- Public deployment recipes (Dockerfile/systemd/reverse proxy) — doc-only follow-up.
- Multi-tenant credential isolation — single-tenant only.
- Legacy SSE transport (`mcp.server.sse`) — being deprecated upstream; do **not** add.
- WebSocket transport — defer until a real client needs it.
- mTLS, JWT, OAuth-on-the-HTTP-layer — Bearer is the v1 contract.
- CORS — ChatGPT Connector is server-to-server; revisit only if a browser client
  appears.

### Success Criteria

- [ ] `MCP_TRANSPORT=stdio` (default) → existing 247 tests pass unchanged.
- [ ] `MCP_TRANSPORT=http` → server boots an ASGI app exposing `POST /mcp`,
      `GET /mcp`, `DELETE /mcp`, with all 5 tools callable via JSON-RPC 2.0.
- [ ] Bearer auth: missing token → `401`; wrong token → `401` (compared with
      `secrets.compare_digest`); correct token → request proceeds.
- [ ] Loopback guard: `MCP_HTTP_HOST=0.0.0.0` + empty `MCP_HTTP_BEARER_TOKEN` →
      server refuses to start (exits non-zero with explicit stderr message).
- [ ] `Recorder` (audit/usage/insight) records HTTP-transport tool calls with the
      same shape as stdio calls.
- [ ] **No** secrets/tokens in any log line at any level (existing redaction
      stays the only path).
- [ ] `ruff check src/ tests/` and `mypy src/ tests/` clean.
- [ ] Total test count ≥ 247 + new transport tests.

## All Needed Context

> **Hard cap: ≤ 200 lines.** Cite files with line ranges. The implementing agent will
> read referenced files directly.

### Documentation & References

```yaml
- url: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
  why: Streamable HTTP wire format — single endpoint, JSON-RPC 2.0, Mcp-Session-Id
  critical: Server returns Mcp-Session-Id header on initialize; clients echo it
            on every subsequent request. Without it sessions get stuck.

- url: https://www.starlette.io/middleware/
  why: BaseHTTPMiddleware vs pure-ASGI middleware
  critical: Pure-ASGI middleware (function) is preferred for short-circuit auth
            because BaseHTTPMiddleware adds a buffering layer that breaks SSE
            streams used by Streamable HTTP for long-running tool responses.

- url: https://www.uvicorn.org/server-behavior/#signal-handlers
  why: uvicorn installs its own SIGINT/SIGTERM handlers
  critical: Don't double-install loop.add_signal_handler in the http branch —
            uvicorn.Server(...).serve() handles graceful shutdown itself.

- file: src/server.py
  why: Current bootstrap; lines 251-313 contain main() that we extract from.
  lines:
    - 13-28  (project root + .env loading; KEEP at top of new server.py)
    - 251-313 (current main(); transport-specific block lives at 295-310)
    - 316-324 (_log / _log_error helpers; reuse them)

- file: src/events/recorder.py
  why: Public-class API + from_env() pattern to mirror for transport modules
  lines: 30-75  (Recorder.__init__, from_env, start/stop)

- file: src/events/writers.py
  why: Async lifecycle (start/stop in try/finally) — mirror for HTTP shutdown
  lines: 46-68  (start) and 71-85 (stop)

- file: PRPs/phase5-tools.md
  why: Tool wiring template (ToolContext + ToolRegistry + _build_server)
  lines: 100-110 (server wiring section)

- file: .venv/lib/python3.14/site-packages/mcp/server/streamable_http_manager.py
  why: SDK API surface for the HTTP session manager
  lines:
    - 30-77   (class docstring + __init__ signature)
    - 98-138  (run() async-context lifecycle — ONE-SHOT per instance)
    - 139-150 (handle_request ASGI signature)

- doc: CLAUDE.md
  section: "Response Format Conventions"
  critical: Tool responses are unchanged — same {data, metadata} or {error}.
            Transport changes only the wire format around them.

- doc: CLAUDE.md
  section: "Debug & Logging Strategy"
  critical: Even though stdout is no longer reserved in HTTP mode, KEEP all logs
            on stderr so the same code path serves both transports safely.

- doc: CLAUDE.md
  section: "Activity Logging (`src/events/`)"
  critical: Recorder.from_env() is the only construction path; no second instance
            allowed (file corruption). Both transports share the SAME recorder.
```

### Current Codebase tree (relevant subset)

```
src/
├── server.py              # current entry point (stdio-only)
├── auth/                  # OAuth + keyring (unchanged by Phase 8)
├── events/                # Recorder, writers, redaction (unchanged)
├── gateway/               # Rest/GraphQL clients (unchanged)
├── tools/                 # 5 tool handlers + ToolContext + ToolRegistry (unchanged)
└── config.py              # api_configs.json loader (unchanged)
tests/
├── test_server.py         # bootstrap + _build_oauth_configs tests
├── auth/ events/ gateway/ tools/ scripts/ integration/  # all stay green
```

### Desired Codebase tree (files to add/modify)

```
src/transport/__init__.py            — re-exports run_stdio + run_http
src/transport/stdio.py               — extracted stdio_server() block; async run_stdio(server, recorder)
src/transport/http.py                — Starlette app, Bearer middleware, loopback guard, async run_http(server, recorder)
src/server.py                        — simplified: builds context+server+recorder, dispatches on MCP_TRANSPORT
tests/transport/__init__.py
tests/transport/test_dispatcher.py   — env-var routing (MCP_TRANSPORT={stdio,http,unknown})
tests/transport/test_http.py         — ASGI test client: 401 paths, loopback guard, round-trip tools/list
.env.example                         — +MCP_TRANSPORT, MCP_HTTP_HOST, MCP_HTTP_PORT, MCP_HTTP_BEARER_TOKEN
requirements.txt                     — promote uvicorn + starlette from transitive to direct deps
README.md                            — new "HTTP Transport" subsection under Connecting / Quickstart
CLAUDE.md                            — "Transport Selection" subsection in Architecture area
```

### Known Gotchas (feature-specific)

```python
# CRITICAL: StreamableHTTPSessionManager.run() is ONE-SHOT per instance.
#   .venv/.../streamable_http_manager.py:117-122 raises RuntimeError if .run() is
#   called twice. Build a fresh manager per process; never reuse across reloads
#   in tests — each test that exercises HTTP needs its own manager.

# CRITICAL: Use pure-ASGI middleware for the Bearer check, not BaseHTTPMiddleware.
#   BaseHTTPMiddleware buffers the response body, breaking the SSE stream that
#   Streamable HTTP can use for long tool calls. Pattern:
#       async def auth_middleware(scope, receive, send): ...
#   wrapped via Starlette's Middleware(...) in app construction.

# CRITICAL: secrets.compare_digest only accepts equal-length byte strings without
#   raising — but it WILL raise TypeError on mismatched types. Always coerce both
#   sides to bytes (`.encode()`) before comparing.

# CRITICAL: uvicorn installs its own SIGINT/SIGTERM handlers via
#   uvicorn.Server(...).serve(). The existing main() in src/server.py installs
#   loop.add_signal_handler — that path is fine for stdio, but in HTTP mode let
#   uvicorn own signals (do NOT call add_signal_handler in the http branch).

# CRITICAL: Recorder must be started ONCE before either transport runs and
#   stopped ONCE after. Don't move recorder lifecycle into the transport modules
#   — keep it in server.py main() so both branches share the same instance.
```

## Implementation Blueprint

### Tasks (in order)

```yaml
Task 1 — Extract stdio bootstrap:
  CREATE src/transport/stdio.py:
    - async def run_stdio(server: Server, shutdown_event: asyncio.Event) -> None
    - Body = current src/server.py:295-310 (the `async with stdio_server() as ...` block)
    - MIRROR pattern from: src/events/writers.py:46-68 (async start with try/finally)

Task 2 — Add HTTP transport:
  CREATE src/transport/http.py:
    - async def run_http(server: Server, shutdown_event: asyncio.Event) -> None
    - Builds StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)
    - Builds Starlette app with:
        * lifespan that wraps `async with manager.run(): yield`
        * single Mount("/mcp", manager.handle_request) (or equivalent ASGI route)
        * Pure-ASGI Bearer middleware (see Gotchas)
    - Reads env: MCP_HTTP_HOST, MCP_HTTP_PORT, MCP_HTTP_BEARER_TOKEN
    - Loopback guard: raise SystemExit(1) with stderr message if non-loopback host + empty token
    - Runs uvicorn.Server(uvicorn.Config(app, host=..., port=..., log_config=None)).serve()
    - log_config=None — let our existing _log() / _log_error() own stderr formatting
  KEY DECISION: pure-ASGI middleware (NOT BaseHTTPMiddleware) — see Gotchas comment.

Task 3 — Dispatcher in server.py:
  MODIFY src/server.py main():
    - After server/recorder setup, branch on os.getenv("MCP_TRANSPORT", "stdio").lower()
    - "stdio"  → await run_stdio(server, shutdown_event)
    - "http"   → await run_http(server, shutdown_event)
    - else     → _log_error(f"Unknown MCP_TRANSPORT={value!r}; use 'stdio' or 'http'") + sys.exit(1)
    - SIGINT/SIGTERM handler stays for stdio path; in http path uvicorn owns signals

Task 4 — Public API:
  CREATE src/transport/__init__.py:
    - from .stdio import run_stdio
    - from .http import run_http
    - __all__ = ["run_stdio", "run_http"]

Task 5 — Tests:
  CREATE tests/transport/test_dispatcher.py:
    - test_unknown_transport_exits_with_clear_message  (capsys + monkeypatch env)
    - test_stdio_default_when_env_unset
    - test_http_branch_invokes_run_http  (monkeypatch run_http to a spy)
  CREATE tests/transport/test_http.py:
    - Use httpx.ASGITransport against the Starlette app (no real network)
    - test_missing_bearer_returns_401
    - test_wrong_bearer_returns_401
    - test_correct_bearer_returns_200_on_initialize
    - test_loopback_guard_refuses_public_bind_without_token  (raises SystemExit)
    - test_tools_list_round_trip  (initialize → tools/list → assert all 5 names)
    - test_no_secrets_in_recorder_jsonl  (run a fetch_data via HTTP, grep audit/insight files for token)

Task 6 — Env, requirements, docs:
  MODIFY .env.example:
    - Add MCP_TRANSPORT, MCP_HTTP_HOST, MCP_HTTP_PORT, MCP_HTTP_BEARER_TOKEN with comments
  MODIFY requirements.txt:
    - Add uvicorn>=0.30 and starlette>=0.40 (currently transitive via mcp; promote to explicit)
  MODIFY README.md:
    - New "HTTP Transport" subsection under Connecting; show env-var setup + curl example
    - Roadmap row Phase 8 → ✅ done with test count update
  MODIFY CLAUDE.md:
    - "Transport Selection" subsection — env var + invariants + reference to src/transport/
```

### Integration Points

```yaml
RECORDER:
  source: src.events.Recorder.from_env()
  lifecycle: started in server.py:main() BEFORE transport runs; stopped in finally
  shared: same instance used by both stdio and http branches

CONFIG:
  source: src.config.load_api_configs (unchanged)
  env_added:
    - MCP_TRANSPORT          (default: stdio; values: stdio | http)
    - MCP_HTTP_HOST          (default: 127.0.0.1)
    - MCP_HTTP_PORT          (default: 8080)
    - MCP_HTTP_BEARER_TOKEN  (no default; required for non-loopback bind)

LOGGING:
  destination: stderr only (both transports)
  level: MCP_LOG_LEVEL env var (default INFO; existing)
  http_specific: uvicorn log_config=None to suppress uvicorn's default handlers
                 — our _log() / _log_error() own formatting

TOOLS:
  context: src.tools.ToolContext (unchanged)
  invariant: tool handlers are transport-agnostic — they accept arguments dict and
             return response dict. Transport layer is purely wire-format.

ERROR CODES:
  reused (no new codes needed):
    - VALIDATION_ERROR  (unknown MCP_TRANSPORT)
  new HTTP-specific surface (not in CLAUDE.md table; HTTP-status-only, no error envelope):
    - 401  → missing/wrong Bearer token
    - 400  → malformed JSON-RPC (handled by SDK)
    - 404  → wrong path (handled by Starlette)
```

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

Zero errors. Do not silence with `# type: ignore` / `# noqa` without a one-line comment.

### Level 2 — Unit tests

```bash
pytest tests/transport/ -v   # debug new tests in isolation first
pytest tests/ -v             # full suite must pass — baseline 247 + new tests
```

Required test categories (per Task 5):
- happy path: full round-trip via ASGI test client
- invalid input → 401 (missing token, wrong token)
- loopback guard: non-loopback bind + empty token → SystemExit
- async path with start/stop in `try/finally` (StreamableHTTPSessionManager lifecycle)
- secret-omission: grep `logs/audit/*.jsonl` and `logs/insight/*.jsonl` produced by an
  HTTP test for the literal Bearer token — must return zero matches.

### Level 3 — Smoke test

```bash
# stdio default still works
echo "" | timeout 2 python -m src.server 2>/tmp/mcp_stdio.log
test -z "$(echo '' | timeout 2 python -m src.server 2>/dev/null)"  # stdout empty

# http boots and 401s missing-token requests
MCP_TRANSPORT=http MCP_HTTP_BEARER_TOKEN=test123 \
  python -m src.server &
SRV=$!
sleep 1
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "Content-Type: application/json" -X POST http://127.0.0.1:8080/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'   # expect 401
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer test123" \
  -H "Content-Type: application/json" -X POST http://127.0.0.1:8080/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}'  # expect 200
kill $SRV
```

## MCP Security Checklist

- [ ] **No secrets in logs (any level).**
      `grep -riE 'authorization:|bearer |client_secret|access_token|api_key' logs/`
      → must return zero matches with non-`<redacted>` values.
- [ ] **All HTTP traffic logging routes through `src/events/redaction.py`** — Phase 8
      adds NO new redaction logic; existing helpers cover request/response bodies.
- [ ] **`MCP_HTTP_BEARER_TOKEN` is read once at startup, compared with
      `secrets.compare_digest`** (constant-time). Never logged.
- [ ] **Credentials read/written ONLY via `keyring`** — Phase 8 does not touch this.
- [ ] **OAuth callback server (Phase 3)** is unchanged; loopback-only, lifetime
      bounded by the `scripts/oauth_login.py` invocation.
- [ ] **Tool responses contain zero auth fields** — covered by existing tools tests;
      Phase 8 wraps responses in JSON-RPC envelopes only.
- [ ] **Pydantic validates every tool input** — unchanged; tool registry already
      validates via `*Input` models.
- [ ] **Error messages do not leak internals** — 401 body must not echo the supplied
      token; loopback-guard exit message must not echo the token value.
- [ ] **`uvicorn` and `starlette` promoted to explicit deps in `requirements.txt`** —
      flagged here since they were transitive (came in via `mcp`).
- [ ] **Stdout has zero non-protocol output** — uvicorn `log_config=None` ensures
      uvicorn does not print to stdout; our `_log()` / `_log_error()` use stderr only.
- [ ] **Loopback guard verified**: launching with `MCP_HTTP_HOST=0.0.0.0` and empty
      `MCP_HTTP_BEARER_TOKEN` exits non-zero with a clear stderr message.

## Risks

1. **`StreamableHTTPSessionManager.run()` is one-shot per instance**
   (`.venv/.../streamable_http_manager.py:117-122` raises if called twice).
   *Recovery:* construct a fresh manager inside `run_http()` per process invocation;
   in tests that exercise HTTP, build a new manager + fresh Starlette app per test
   (use a pytest fixture, not a module-level instance).

2. **uvicorn signal handling overlaps with the existing
   `loop.add_signal_handler` calls in `src/server.py:286-287`**.
   uvicorn installs its own SIGINT/SIGTERM handlers; double-handling can leave the
   server hung on shutdown. *Recovery:* keep `add_signal_handler` only on the stdio
   branch. The http branch lets `uvicorn.Server(...).serve()` own signals end-to-end;
   don't pass shutdown_event into uvicorn.

3. **Pure-ASGI middleware semantics differ from `BaseHTTPMiddleware`** — the latter
   buffers responses, breaking the SSE stream that Streamable HTTP can return for
   long tool calls. *Recovery:* implement the Bearer check as a plain ASGI callable
   (`async def (scope, receive, send)`) that short-circuits with a 401 on
   mismatch, otherwise delegates to the inner app. Reference Starlette's
   "Pure ASGI middleware" docs.

## Final Checklist

- [ ] `ruff check src/ tests/` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all pass (baseline 247 + Phase 8 transport tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] Smoke test (Level 3) passes both branches
- [ ] Acceptance criteria from "Success Criteria" — all checked
- [ ] `MCP_TRANSPORT=stdio` is still the default; existing Claude Desktop / Codex CLI
      configs work without any change
