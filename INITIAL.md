# Feature Request

> One feature per file. Run `/generate-prp INITIAL.md` then review the produced PRP and
> run `/execute-prp PRPs/{...}.md`.
>
> **This file is a DELTA**: it does NOT repeat info already in [docs/plan.md](docs/plan.md)
> or [CLAUDE.md](CLAUDE.md). It captures only what's specific or deeper than the roadmap.

---

## STATUS

**Phases 1–7 shipped.** GitHub OAuth integration verified end-to-end against Claude
Desktop **and** Codex CLI (both stdio transport).

| Phase | PRP | Status |
|-------|-----|--------|
| 3 — Authentication | [PRPs/phase3-auth.md](PRPs/phase3-auth.md) | ✅ Done |
| 4 — API Gateway | [PRPs/phase4-gateway.md](PRPs/phase4-gateway.md) | ✅ Done |
| 5 — Tools & Integration | [PRPs/phase5-tools.md](PRPs/phase5-tools.md) | ✅ Done |
| 6 — Testing & Documentation | [PRPs/phase6-testing-docs.md](PRPs/phase6-testing-docs.md) | ✅ Done |
| **GitHub OAuth integration** | (no PRP — direct edit) | ✅ Done — `scripts/oauth_login.py`, `config/api_configs.json`, real-world OAuth flow verified |

**Total test count: 247 passing** (was 232 at v0 ship; +10 oauth_login tests + 5
helper unit tests in this delta).

---

## NEXT FEATURE — Phase 8: HTTP Transport

### FEATURE
Add an HTTP transport to the MCP Data Gateway so the server can be reached by
remote clients (ChatGPT Connectors, web-based MCP clients) **alongside** the
existing stdio transport. Selectable via env var; stdio remains the default.

### ACCEPTANCE CRITERIA
- `MCP_TRANSPORT=stdio` (default) → existing behavior is unchanged. All 247
  tests pass without modification.
- `MCP_TRANSPORT=http` → server boots an ASGI app exposing the MCP
  Streamable HTTP endpoint at `MCP_HTTP_HOST:MCP_HTTP_PORT` (defaults
  `127.0.0.1:8080`).
- A Bearer-token auth middleware guards every HTTP request when
  `MCP_HTTP_BEARER_TOKEN` is set; missing/invalid token returns `401`. If the
  env var is unset and `MCP_HTTP_HOST` is not loopback, the server refuses to
  start (fail-loud — no unauthenticated public bind).
- All five tools (`list_apis`, `fetch_data`, `send_data`, `execute_graphql`,
  `get_status`) work identically over HTTP.
- The Recorder (audit/usage/insight/debug) records HTTP-transport calls the
  same way it records stdio calls.
- New tests cover: transport selection, Bearer auth happy + fail paths,
  loopback-only refuse-to-start guard, and a full tool-call round-trip via
  ASGI test client.

### EDGE CASES
- **OAuth callback in HTTP mode**: when the server runs remote, the existing
  `127.0.0.1:8765/callback` is no longer reachable from the user's browser.
  Out-of-band: continue requiring `scripts/oauth_login.py` to be run **on the
  user's machine**, then upload the resulting keychain entry — OR document a
  public-callback variant. **Decide in PRP**, default for Phase 8 is "OAuth
  setup remains local; HTTP server consumes already-stored tokens."
- **Multi-tenant token sharing**: a single keychain entry per `api_id` is
  shared across all HTTP clients of that server instance. Single-tenant
  deployment is the explicit assumption. Multi-tenant support is OUT OF SCOPE
  for Phase 8.
- **CORS**: ChatGPT Connector uses server-to-server calls (no browser CORS).
  No CORS middleware in Phase 8 unless a concrete browser-client requirement
  appears.
- **Activity log volume**: HTTP mode can drive higher concurrency than stdio.
  Existing queue overflow handling (drop with stderr warning, FIFO) covers
  this; no new code needed.
- **Logging stdout vs stderr**: under stdio, stdout is reserved for JSON-RPC.
  Under HTTP, that constraint is lifted — but keep all logs on stderr anyway
  for consistency and to stay safe under future transport-toggle scenarios.

### OUT OF SCOPE
- Public deployment recipes (Dockerfile, systemd, reverse proxy) — separate
  doc-only follow-up after Phase 8 lands.
- Multi-tenant credential isolation.
- WebSocket transport (`mcp.server.websocket`) — defer until a real client
  needs it.
- Legacy SSE transport (`mcp.server.sse`) — being deprecated upstream; do not
  add.
- mTLS / JWT auth — Bearer is the v1 contract; richer auth via PRP delta later.

### REFERENCE PATTERNS
- **Reference implementation**: `src/events/` (per CLAUDE.md). New code lives
  under a dedicated `src/transport/` package, mirroring `src/events/` layout
  (one module per concern, public API via `__init__.py`, tests under
  `tests/transport/`).
- **Existing stdio bootstrap**: `src/server.py` lines around the
  `stdio_server()` async-context block. Refactor into
  `src/transport/stdio.py` and add a sibling `src/transport/http.py` —
  `src/server.py` then dispatches based on `MCP_TRANSPORT`.
- **MCP SDK transports installed**: `mcp.server.streamable_http`,
  `mcp.server.streamable_http_manager` (modern HTTP, Streamable HTTP spec),
  plus the existing `mcp.server.stdio`. Use Streamable HTTP, not legacy SSE.
- **Auth middleware pattern**: a small Starlette/ASGI middleware that reads
  `Authorization: Bearer <token>`, compares to `MCP_HTTP_BEARER_TOKEN` (using
  `secrets.compare_digest`), short-circuits with `401` on mismatch.
- **Env-var docs**: extend `.env.example` and the README env-var table the
  same way previous phases did.

### NEW DEPENDENCIES
- `uvicorn` (ASGI server) and `starlette` (ASGI framework) — add to
  `requirements.txt` as runtime deps. (Both are mature, security-maintained,
  and used by the upstream MCP SDK's HTTP examples.)

### VALIDATION GATES (run during `/execute-prp`)
```bash
ruff check src/ scripts/ tests/ --fix
ruff format src/ scripts/ tests/
mypy src/ scripts/ tests/
pytest tests/ -v          # must keep 247 passing + add new transport tests
```

### EXPECTED DELIVERABLES
- `src/transport/__init__.py`
- `src/transport/stdio.py` (extracted from `src/server.py`)
- `src/transport/http.py` (new ASGI app + auth middleware)
- `src/server.py` simplified to a transport dispatcher
- `tests/transport/__init__.py`
- `tests/transport/test_http.py` (Bearer auth, loopback guard, round-trip)
- `tests/transport/test_dispatcher.py` (env-var-driven selection)
- `requirements.txt` (+`uvicorn`, +`starlette` as runtime deps — both used by
  the upstream MCP SDK's HTTP examples)
- `pyproject.toml` (no change required for deps — they're runtime, not extras —
  but `[tool.ruff].src` and `[tool.mypy].files` already include `src/`/`tests/`
  so the new `src/transport/` and `tests/transport/` packages are picked up
  automatically)
- `.env.example` (+`MCP_TRANSPORT`, `MCP_HTTP_HOST`, `MCP_HTTP_PORT`,
  `MCP_HTTP_BEARER_TOKEN`)
- `README.md` (new "HTTP Transport" section + roadmap row)
- `CLAUDE.md` ("Transport Selection" subsection in the architecture or
  workflow area)
