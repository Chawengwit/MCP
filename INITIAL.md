# Feature Request

> One feature per file. Run `/generate-prp INITIAL.md` then review the produced PRP and run
> `/execute-prp PRPs/{...}.md`.
>
> **This file is a DELTA**: it does NOT repeat info already in [docs/plan.md](docs/plan.md)
> or [CLAUDE.md](CLAUDE.md). It captures only what's specific or deeper than the roadmap —
> acceptance criteria, edge cases, things-to-watch-for, and out-of-scope clarifications.

---

## FEATURE

**Phase 2 — Core MCP Server Bootstrap.** See [docs/plan.md § Phase 2](docs/plan.md) for the
high-level scope. This delta adds:

- **Tool registry** so Phase 5 can add tools without editing `src/server.py`.
  Shape: `dict[str, ToolSpec]` where `ToolSpec` bundles `name`, `description`,
  `input_schema`, and `handler`.
- **Config loader (`src/config.py`)**: reads `config/api_configs.json`, validates with
  Pydantic, substitutes `${VAR_NAME}` placeholders from environment.
- **First tool: `list_apis`** — proves the registry + Recorder integration end-to-end
  without needing auth (Phase 3) or the gateway (Phase 4). Lists configured APIs but
  **omits all auth fields** in the response.
- **Recorder integration in the call path**: every tool invocation produces one `audit` +
  one `usage` + one `insight` event automatically (so Phase 5 tools don't reinvent it).
- **Graceful shutdown**: SIGINT/SIGTERM drains the Recorder queue before exit.

## ACCEPTANCE CRITERIA

- [ ] `python -m src.server` starts; logs to **stderr** only.
- [ ] `list_apis` returns `{"data": [...], "metadata": {...}}` with each item containing
      only `name`, `type`, `base_url`, `endpoints` (list of names) — no `auth`, `logging`,
      `limits`.
- [ ] One `audit` + one `usage` + one `insight` event written per `list_apis` call.
- [ ] Config loader handles 4 cases: file present + valid, file missing (warn + empty),
      invalid JSON (raise), unresolved `${VAR}` (raise).
- [ ] All 27 existing events tests still pass.
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest tests/` all green.

## EDGE CASES & GOTCHAS NOT IN docs/plan.md

- **`api_configs.json` top-level `_comment` field** must be accepted (use
  `extra="ignore"` on the top-level Pydantic model — see
  [`config/api_configs.example.json`](config/api_configs.example.json)).
- **`${VAR}` substitution is single-pass.** If env var X resolves to literal `"${Y}"`,
  do NOT re-scan — this prevents user data from being interpreted as templates.
- **`loop.add_signal_handler` raises `NotImplementedError` on Windows.** Wrap in try/except
  and fall back to default Ctrl-C behaviour.
- **`from_env()` reads env vars at construction time only.** Tests that need a custom log
  dir must `monkeypatch.setenv("MCP_LOG_DIR", ...)` BEFORE calling `Recorder.from_env()`.
- **Tool handler exceptions** must be caught in the server wrapper and converted to the
  `{error: {code, message, details}}` shape — never let a Python exception bubble back
  to the MCP client.

## OUT OF SCOPE FOR PHASE 2

- HTTP client / real API calls (Phase 4)
- OAuth flows / keyring integration (Phase 3)
- `fetch_data`, `send_data`, `execute_graphql` tools (Phase 5)
- `get_status` tool — depends on auth state (defer to Phase 3)
- Rate limiting / response caching (out of scope project-wide)

## REFERENCE PATTERNS (mirror these — see CLAUDE.md "Reference Implementation")

- `src/events/recorder.py` — public class with `from_env()`; explicit start/stop.
- `src/events/writers.py` — async queue + sentinel-drain on stop.
- `src/events/schemas.py` — Pydantic v2 conventions.
- `tests/events/test_recorder.py` — pytest layout (auto async mode, no decorator).
