name: "Phase 2 — MCP Server Bootstrap"
description: |
  Core server initialization with tool registry, config loader, list_apis tool,
  Recorder integration, and graceful shutdown. Foundation for Phase 5 tools.

---

## Goal

Implement Phase 2 of the MCP Data Gateway: a bootstrapped MCP server that can start,
load API configurations, list them via the `list_apis` tool, record all activity via
the `Recorder`, and shut down gracefully on SIGINT/SIGTERM. The tool registry pattern
enables Phase 5 to add tools without modifying `src/server.py`.

## Why

- **Fulfills [docs/plan.md § Phase 2](../../docs/plan.md)** — Core server initialization,
  tool schema + registry, logging infrastructure integration.
- **Enables Phase 5** — Tools (fetch_data, send_data, execute_graphql) depend on the
  registry + Recorder call-path integration to record audit/usage/insight events.
- **Foundation for graceful operation** — Signal handlers ensure the Recorder queue
  drains before exit; no dropped events.

## What

### Success Criteria

- [ ] `python -m src.server` starts and runs without error; logs to **stderr** only.
- [ ] Config loader successfully reads/validates `config/api_configs.json`; rejects invalid
      JSON and unresolved `${VAR}` placeholders; warns if file missing.
- [ ] Tool registry pattern works: `dict[str, ToolSpec]` with name/description/input_schema/handler.
- [ ] `list_apis` tool returns `{"data": [...], "metadata": {...}}` with each API containing
      only `name`, `type`, `base_url`, `endpoints` — no `auth`, `logging`, `limits`.
- [ ] Every `list_apis` call records exactly one `audit`, one `usage`, one `insight` event.
- [ ] SIGINT/SIGTERM gracefully drain Recorder and exit (test via `timeout`).
- [ ] All 27 existing events tests pass; new tests added for config, registry, tools.
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest tests/` all green.

## All Needed Context

### Documentation & References

```yaml
# MCP SDK
- lib: mcp
  docs: https://modelcontextprotocol.io/docs/concepts/tools
  why: Tool schema definition, server initialization, stdio server
  critical: |
    Tool input_schema must be JSONSchema (dict), not Pydantic model directly.
    Server uses stdio_server for JSON-RPC communication.

# Pydantic v2 (mirror src/events/schemas.py:1–30)
- file: src/events/schemas.py
  lines: 1-30
  why: Pydantic v2 import patterns, ConfigDict usage, validators

# Recorder API (mirror src/events/recorder.py)
- file: src/events/recorder.py
  lines: 1-70
  why: from_env() pattern, async start/stop, record_* method signatures

# Config format
- file: config/api_configs.example.json
  lines: 1-89
  why: Structure of APIs, auth, endpoints, logging blocks
  critical: |
    Top-level _comment field must be accepted — use extra="ignore".
    ${VAR} substitution must be single-pass (no recursive resolution).

# Global rules
- doc: CLAUDE.md
  sections:
    - "Response Format Conventions" (success: data+metadata; error: error+code+message+details)
    - "Standard Error Codes" (API_NOT_CONFIGURED, ENDPOINT_NOT_FOUND, etc.)
    - "Activity Logging > Recording Rules" (tool must call record_audit + record_usage + record_insight)
    - "Things to Watch For" (signal handlers on Windows, keyring availability)
```

### Current Codebase Tree

```
src/
├── __init__.py
├── server.py                 (to create)
├── config.py                 (to create)
├── events/                   (✓ exists, 27 tests pass)
│   ├── __init__.py
│   ├── recorder.py
│   ├── writers.py
│   ├── schemas.py
│   ├── redaction.py
│   └── retention.py
├── tools/                    (to create)
│   ├── __init__.py
│   ├── spec.py              (ToolSpec dataclass)
│   ├── registry.py          (tool registry pattern)
│   └── builtin.py           (list_apis implementation)
├── auth/                     (planned for Phase 3)
├── gateway/                  (planned for Phase 4)
└── models/                   (planned for Phase 5)

tests/
├── events/                   (✓ 27 passing tests)
├── test_config.py            (to create)
├── test_server.py            (to create)
└── tools/
    ├── test_registry.py      (to create)
    └── test_builtin.py       (to create)
```

### Desired Codebase Tree

```
src/server.py                  — MCP server bootstrap; signal handling; Recorder lifecycle
src/config.py                  — Load/validate config/api_configs.json; ${VAR} substitution
src/tools/spec.py              — ToolSpec dataclass; input_schema as JSONSchema dict
src/tools/registry.py          — Tool registry: dict[str, ToolSpec]; registration API
src/tools/builtin.py           — list_apis tool implementation
src/tools/__init__.py          — Export public registry API
tests/test_config.py           — Config loader: valid, missing, invalid JSON, bad ${VAR}
tests/test_server.py           — Server startup/shutdown; stdin reader integration
tests/tools/test_registry.py   — Registry add/lookup; invalid registrations rejected
tests/tools/test_builtin.py    — list_apis: correct shape; audit/usage/insight recorded
```

### Known Gotchas (feature-specific)

```python
# CRITICAL: loop.add_signal_handler raises NotImplementedError on Windows
#   Wrap in try/except; fall back to default Ctrl-C if unsupported.
#   See src/server.py task.

# CRITICAL: ${VAR} substitution is single-pass
#   If an env var resolves to literal "${ANOTHER_VAR}", do NOT re-scan.
#   Prevents user data being interpreted as templates.
#   See src/config.py task.

# CRITICAL: Recorder.from_env() reads env vars at construction time only
#   Tests that need custom MCP_LOG_DIR must monkeypatch BEFORE Recorder.from_env().
#   See tests/test_server.py task.

# MCP tool input_schema must be JSON-serializable dict, not Pydantic model
#   Use pydantic.json_schema.model_json_schema() to convert.
#   See src/tools/spec.py task.

# Config loader must accept _comment field at top level
#   Use extra="ignore" on top-level Pydantic model.
#   See src/config.py task.

# Tool handler exceptions must be caught and converted to error response shape
#   Never let Python exception bubble to MCP client.
#   See src/server.py tool_handler task.
```

## Implementation Blueprint

### Data Models

```python
# src/tools/spec.py

ToolSpec:
  name: str
  description: str
  input_schema: dict  # JSONSchema, not Pydantic model
  handler: Callable   # async (session_id: UUID, **kwargs) -> dict

# src/config.py

ApiConfig:
  type: "rest" | "graphql"
  base_url: str
  auth: dict | None
  endpoints: dict[str, dict]  # endpoint name → config
  logging: dict | None
  limits: dict | None

ApiConfigsRoot:
  apis: dict[str, ApiConfig]
  # extra="ignore" so _comment field is accepted
```

### Tasks (in order)

```yaml
Task 1 — Config Loader (src/config.py):
  CREATE src/config.py:
    - Load JSON from config/api_configs.json
    - Pydantic v2 validation with ApiConfig + ApiConfigsRoot
    - ${VAR_NAME} substitution from environment (single-pass)
    - Error handling: missing file (warn + return empty), invalid JSON (raise), unresolved ${} (raise)
    - Public function: load_api_configs(path: Path | None = None) -> dict[str, ApiConfig]
    - MIRROR pattern from: src/events/recorder.py:33–57 (from_env classmethod pattern)
  KEY DECISION: Use extra="ignore" on ApiConfigsRoot to accept _comment field

Task 2 — Tool Spec & Registry (src/tools/):
  CREATE src/tools/spec.py:
    - ToolSpec dataclass: name, description, input_schema (dict), handler (Callable)
    - Pydantic v2 imports (from __future__ import annotations)
    - MIRROR pattern from: src/events/schemas.py:1–10 (Pydantic v2 style)
  CREATE src/tools/registry.py:
    - Registry class: dict[str, ToolSpec] with add/get methods
    - Public function: get_registry() -> dict[str, ToolSpec]
    - Allow registration only at startup (no runtime mutation)
  CREATE src/tools/__init__.py:
    - Export Registry, ToolSpec, get_registry

Task 3 — list_apis Tool (src/tools/builtin.py):
  CREATE src/tools/builtin.py:
    - list_apis(session_id: UUID, recorder: Recorder, api_configs: dict[str, ApiConfig]) → dict
    - For each API, return only: name, type, base_url, endpoints (list of names)
    - OMIT: auth, logging, limits
    - Response shape per CLAUDE.md: {"data": [...], "metadata": {...}}
    - Call recorder.record_audit() + record_usage() + record_insight()
    - Catch handler exceptions; convert to error shape {"error": {code, message, details}}
    - MIRROR pattern from: src/events/recorder.py:65–93 (record_audit signature)
  UPDATE src/tools/__init__.py:
    - Export register_builtin_tools() function

Task 4 — MCP Server Bootstrap (src/server.py):
  CREATE src/server.py:
    - Initialize mcp.Server() with name="mcp-data-gateway"
    - Load config via load_api_configs()
    - Create Recorder via Recorder.from_env()
    - Call recorder.start() in async startup handler
    - Register all tools from registry via server.add_tool()
    - For each tool, wrap handler to catch exceptions and convert to error shape
    - Tool wrapper: extract session_id from context (or generate UUID)
    - Log to stderr only (never stdout — stdout is MCP protocol stream)
    - Use stdio_server(server) for JSON-RPC communication
    - MIRROR pattern from: src/events/recorder.py:30–57 (constructor + from_env)

Task 5 — Graceful Shutdown (src/server.py):
  ADD to src/server.py:
    - Register SIGINT/SIGTERM handlers via loop.add_signal_handler()
    - On signal, call await recorder.stop() (drains queue, closes handles)
    - Wrap in try/except for NotImplementedError (Windows compat)
    - Exit cleanly with sys.exit(0)
    - CRITICAL: Must be async context-aware (use asyncio.get_running_loop())
    - MIRROR pattern from: src/events/writers.py:71–77 (async stop with sentinel)

Task 6 — Tests:
  CREATE tests/test_config.py:
    - Valid config load: parses JSON, validates types, ${VAR} substituted
    - Missing file: warns, returns {}
    - Invalid JSON: raises JSONDecodeError
    - Unresolved ${VAR}: raises ValueError with clear message
    - _comment field: accepted and ignored
    - Happy path + edge cases
  CREATE tests/test_server.py:
    - Server.from_env() starts without error
    - Config loaded at startup
    - Recorder started and stopped properly
    - SIGTERM handled gracefully (use mock.patch on add_signal_handler)
    - Async start/stop in try/finally
  CREATE tests/tools/test_registry.py:
    - Register tool; lookup returns ToolSpec
    - Lookup nonexistent tool raises KeyError or returns None
    - Registry is dict-like
  CREATE tests/tools/test_builtin.py:
    - list_apis returns correct shape: data + metadata
    - Each API in response has only: name, type, base_url, endpoints
    - auth/logging/limits omitted
    - One audit + one usage + one insight event recorded
    - Test with mock Recorder to verify record_* calls
    - Exception in handler converts to error shape
  MIRROR test pattern from: tests/events/test_recorder.py:44–60 (monkeypatch, async fixtures)
  UPDATE tests/events/test_*.py:
    - Ensure all 27 existing tests still pass (no breaking changes)
```

### Integration Points

```yaml
RECORDER:
  source: src.events.Recorder
  calls: One per tool invocation:
    - record_audit(session_id, tool="list_apis", result="success"|"error", duration_ms)
    - record_usage(tool="list_apis", status="success"|"error", duration_ms)
    - record_insight(session_id, tool="list_apis", tool_args={...})
  notes: All three calls per tool, every time, unless disabled via env var.

CONFIG:
  source: src.config.load_api_configs()
  env vars: (none new; existing MCP_LOG_* used by Recorder)
  files: config/api_configs.json (required on disk; missing = warn + empty)

LOGGING:
  destination: stderr only (never stdout)
  format: structured JSON (from Recorder), human-readable startup messages to stderr
  level: MCP_LOG_LEVEL env var (default INFO; set to DEBUG for verbose request tracing)

MCP_SERVER:
  source: mcp.Server
  entry: python -m src.server
  wiring: stdio_server(server) for JSON-RPC over stdio
```

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

Zero errors. Do not silence with `# type: ignore` without a one-line comment.

### Level 2 — Unit tests

```bash
pytest tests/tools/ -v           # new tools tests in isolation
pytest tests/test_config.py -v   # config loader tests
pytest tests/test_server.py -v   # server startup/shutdown
pytest tests/ -v                 # full suite; must include 27 existing events tests
```

All pass. Required test categories:

- **Happy path**: list_apis returns correct shape; config loads; server starts/stops.
- **Invalid input**: bad JSON, missing ${VAR}, unresolved placeholders → specific exceptions.
- **Async lifecycle**: start/stop in try/finally; Recorder queue drains.
- **Security**: No auth/logging/limits fields in list_apis response.

### Level 3 — Smoke test

```bash
# Start server with timeout; verify stderr gets logs, stdout is empty
timeout 2 python -m src.server < /dev/null 2>/tmp/mcp_stderr.log
test -z "$(timeout 2 python -m src.server < /dev/null 2>/dev/null)" || echo "FAIL: non-empty stdout"
grep -q "MCP server started" /tmp/mcp_stderr.log || echo "FAIL: startup log not found"
```

## MCP Security Checklist

- [ ] **No secrets in logs (any level).**
      `grep -riE 'authorization:|bearer |client_secret|access_token|api_key' logs/`
      → Must return zero matches with non-`<redacted>` values.
      Config parser must NOT log auth block at INFO level.

- [ ] **No auth fields in tool responses.**
      `list_apis` response must contain only: name, type, base_url, endpoints (list of names).
      Grep serialized response: `grep -E '"auth"|"client_id"|"token"|"secret"'` → zero matches.

- [ ] **Tool input validation via Pydantic.**
      `list_apis` takes session_id as UUID (no raw string/dict).

- [ ] **Error messages do not leak internals.**
      Tool exceptions converted to `{"error": {code, message, details}}` shape.
      No filesystem paths, env var names, or stack traces in user-facing responses.

- [ ] **Stdout reserved for MCP protocol.**
      All logs, startup messages, errors go to stderr.
      Verify via: `python -m src.server < /dev/null 2>/dev/null` → empty output.

- [ ] **Signal handlers bound to localhost only.**
      Not applicable for Phase 2 (no HTTP server yet); skip for now.

- [ ] **Credentials never in plaintext logs.**
      Config loader logs `{apis: {api_name: {...}}}` at DEBUG level only.
      At INFO level, log count of APIs loaded, not the config itself.

- [ ] **No new pip dependency added without flagging.**
      Phase 2 requires no new dependencies beyond existing (mcp, httpx, keyring, pydantic, etc.).

## Risks

1. **MCP SDK API surface drift** — README examples or docs may not match installed `mcp` version.
   - *Recovery:* Verify `Server`, `stdio_server`, `Tool` schema shape against installed package before writing server wiring. If type errors, check mcp version in requirements.txt and adjust code.

2. **Signal handler not available on Windows** — `loop.add_signal_handler` raises `NotImplementedError`.
   - *Recovery:* Wrap in try/except and log a warning if unsupported. Fall back to default Ctrl-C behavior (no explicit queue drain, but async context manager will still clean up on exit).

3. **Config substitution edge case** — Env var resolves to another `${VAR}` literal; re-scanning would recurse.
   - *Recovery:* Implement as single-pass: scan for `${...}`, extract var name, fetch from os.environ, substitute once. Do NOT re-scan the result.

## Final Checklist

- [ ] `ruff check src/ tests/` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all pass (including 27 existing events tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] Smoke test passes: `timeout 2 python -m src.server` starts, logs to stderr, stdout empty
- [ ] Acceptance criteria from "Success Criteria" — all checked
- [ ] No new pip dependencies added (or explicitly flagged in commit)
