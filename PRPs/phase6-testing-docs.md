# PRP — Phase 6: Testing & Documentation

## Goal

Round out the test pyramid for Phases 3–5 with a small set of integration tests that
exercise the full stack (auth → gateway → tool → Recorder), update `README.md` with
real-world setup instructions and troubleshooting, and replace
`config/api_configs.example.json` with 2–3 realistic API definitions a user can copy.
No new business logic — this phase is about hardening and documentation.

## Why

- This is **Phase 6** of [docs/plan.md](../docs/plan.md).
- Phases 3–5 each have unit tests; what's missing is one integration path that proves
  they fit together as designed.
- The current README and example config were written before keyring, OAuth, and the
  real tool surface existed — they leave a new user stuck on first run.

## What

### `tests/integration/test_full_flow.py` — One end-to-end happy path

A single test that:

1. Starts a real `Recorder` writing to `tmp_path`.
2. Mocks `keyring` (in-memory dict) and `httpx` (respx or `httpx.MockTransport`).
3. **Pre-populates the in-memory keyring** with a valid `TokenInfo`
   (`access_token="test_token_xyz"`, `expires_at=time.time()+3600`) for the test
   `api_id`. The OAuth flow is NOT exercised in the integration test — Phase 3 unit
   tests already cover that path. This keeps the integration test deterministic and
   fast (no browser, no callback server, no real network).
4. Configures one fake REST API in a temp `api_configs.json` (auth.type=oauth2).
5. Calls `fetch_data(api_id="example", endpoint="get_users")`.
6. Asserts:
   - Response shape matches `{data, metadata}`.
   - One JSONL line each in `audit/`, `usage/`, `insight/` for the call.
   - `grep` of every JSONL line for `bearer|client_secret|access_token|api_key|password`
     (with non-`<redacted>` values) returns zero matches.
   - The literal token string `"test_token_xyz"` does NOT appear in any JSONL line.
   - `Authorization: Bearer test_token_xyz` was sent to the mocked endpoint
     (verified via the mock transport's recorded request).

A second test variant: same flow but the keyring is **empty** (no pre-populated token)
→ tool returns `{error: {code: "AUTH_REQUIRED", ...}}` without invoking OAuth (the
mocked `webbrowser.open` is asserted not to be called), and Recorder's `audit` event
has `result="error"`.

### `tests/integration/test_smoke.py` — Server boots and exits

```python
async def test_server_boots_and_exits():
    """Server starts, becomes ready, receives shutdown, drains Recorder, exits."""
    # spawn `python -m src.server` as subprocess with MCP_LOG_DIR=tmp_path
    # send SIGTERM after a short delay
    # assert exit code clean, stdout empty, stderr contains structured log lines
```

### `README.md` updates

Replace or extend these sections:

- **Quickstart** — `pip install -r requirements.txt`, `cp .env.example .env`,
  `cp config/api_configs.example.json config/api_configs.json`,
  `python -m src.server`.
- **Configuring an API** — concrete example for GitHub (REST) and one GraphQL endpoint;
  reference the example file.
- **OAuth setup** — how `OAUTH_CALLBACK_PORT` works; what to register at the provider
  (redirect URI = `http://127.0.0.1:8765/callback`).
- **Keyring setup per OS**:
  - macOS: works out of the box (Keychain).
  - Linux: needs `gnome-keyring` / `kwallet`, or `pip install keyrings.alt`.
  - Windows: works out of the box (Credential Manager).
- **Troubleshooting** — table mirroring `CLAUDE.md § Common Failure Modes`, plus:
  - "OAuth flow opens but never returns" → check `OAUTH_CALLBACK_PORT` not blocked by
    firewall on `127.0.0.1`.
  - "RESPONSE_TOO_LARGE on a binary download" → expected; raise
    `MCP_MAX_RESPONSE_BYTES` or use the API's pagination.
  - "GraphQL response has both `data` and `errors`" → that's intentional, not a bug.
- **Logging** — explain `logs/{audit,debug,usage,insight}/YYYY-MM.jsonl`,
  retention via `MCP_LOG_RETENTION_DAYS`, and that logs are operator-only (no MCP tool
  exposes them to Claude).

### `config/api_configs.example.json` — Refine (do NOT replace)

The current file already has `example_rest_api` (oauth2) and `example_graphql_api`
(bearer). These are intentionally generic + parametrized via `${ENV_VAR}` placeholders.
Phase 6 should **refine** them, not rewrite from scratch. Specifically:

1. **Fix `redirect_uri`** — current value `http://localhost:8765/callback` should be
   `http://127.0.0.1:8765/callback` to match Phase 3's callback bind address (browsers
   sometimes treat localhost vs 127.0.0.1 as different origins).
2. **Add a third no-auth example** — small entry with `"auth": null` to demonstrate the
   "no auth needed" branch added in Phase 5 (e.g. a public REST endpoint).
3. **Expand `_comment` field at top-level** — explain the auth.type values
   (`oauth2`/`bearer`/`api_key`/null), point at README troubleshooting, and remind that
   `redact_fields` is per-API additive on top of the global redaction list.
4. **Keep all existing fields and placeholders** — do not rename `example_rest_api`,
   do not remove `logging` or `limits` blocks, do not switch placeholders to literal
   secrets (provider names like "github" should remain `${EXAMPLE_REST_CLIENT_ID}` style).

Each example must continue to validate against `ApiConfigsRoot` (covered by
`tests/test_example_config.py` in Task 4).

### Success Criteria

- [ ] One integration test asserts full flow + Recorder output + redaction on
      a happy path.
- [ ] One integration test asserts the `AUTH_REQUIRED` failure path produces a
      proper error response and an `audit` event with `result="error"`.
- [ ] One smoke test asserts `python -m src.server` boots and exits cleanly with no
      stdout output.
- [ ] `README.md` Quickstart works verbatim on a fresh checkout (manual verification).
- [ ] `config/api_configs.example.json` validates against `ApiConfigsRoot` Pydantic
      model (add a unit test for this if not already present).
- [ ] Phase 3, 4, 5 unit tests + 27 events tests still pass.
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest tests/` all green.

---

## All Needed Context

### Documentation & References

```yaml
- doc: CLAUDE.md
  section: "Common Failure Modes"
  critical: README troubleshooting must mirror this table; do not contradict it.

- doc: CLAUDE.md
  section: "Activity Logging"
  critical: README logging section must say logs are operator-only — no MCP tool exposes them.

- doc: CLAUDE.md
  section: "Response Format Conventions"
  critical: README "Response shapes" subsection (if added) cites this — no duplication.

- file: src/events/recorder.py
  why: Reference for how the Recorder is created/started/stopped — README explains it.
  lines: full

- file: src/auth/credentials.py  (Phase 3)
  why: README's keyring section reflects CredentialStorageError message verbatim
  lines: full

- file: src/config.py
  why: ApiConfigsRoot model — example config must validate against it
  lines: 13–60

- file: tests/conftest.py
  why: Shared fixtures (tmp_path patterns, monkeypatch for env vars)
  lines: full

- url: https://lundberg.github.io/respx/
  why: Mocking httpx in tests; cleaner than MockTransport for some patterns
  critical: Optional — may already be in requirements-dev. Use httpx.MockTransport otherwise.

- url: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps
  why: Real OAuth provider for the example config (callback URL, scopes)
  critical: Redirect URI = http://127.0.0.1:8765/callback must be registered in the GitHub App.
```

### Current Codebase

```
src/                          ← Phases 2 + 3 + 4 + 5 + 7 complete
tests/
├── auth/                     ← Phase 3 unit tests
├── gateway/                  ← Phase 4 unit tests
├── tools/                    ← Phase 5 unit tests + (this phase) integration tests
└── events/                   ← Phase 7 (27 tests)
config/api_configs.example.json   ← currently minimal — to expand
README.md                     ← currently sparse on setup — to update
```

### Desired Codebase

```
tests/integration/
├── __init__.py
├── conftest.py               ← integration-scoped fixtures (full Recorder, mock keyring, mock httpx)
├── test_full_flow.py         ← one happy path + one auth-failure path
└── test_smoke.py             ← subprocess server boot/exit

README.md                     ← MODIFY: Quickstart, OAuth setup, keyring per OS, Troubleshooting
config/api_configs.example.json   ← REWRITE with 2–3 real examples
tests/test_example_config.py  ← (optional, if not present) validate example file against ApiConfigsRoot
```

### Known Gotchas (Phase 6 specific)

```python
# CRITICAL — Integration test must not hit real network
# A stray real httpx call to api.github.com makes CI flaky and leaks env tokens.
# Fix: install respx or use httpx.MockTransport at the test boundary.
#   Assert no real network call (respx provides this; with MockTransport patch __aenter__).
# File: tests/integration/conftest.py — fixture patches the transport globally for the test.

# CRITICAL — Subprocess smoke test races the server boot
# Spawning `python -m src.server` and immediately sending SIGTERM may kill it before
# Recorder.start() finishes; that's a noise failure, not a real one.
# Fix: wait for a stderr "ready" log line OR sleep 1.5s before SIGTERM.
#   Use timeout=10s on subprocess.communicate().
# File: tests/integration/test_smoke.py.

# CRITICAL — Example config must validate
# Every example written into api_configs.example.json must round-trip through
# ApiConfigsRoot(**json.loads(...)) without ValidationError.
# Fix: small unit test loads the file and instantiates the model.
# File: tests/test_example_config.py — runs as part of regular pytest run.

# CRITICAL — README quickstart paths
# Wrong relative paths in README break the first-run experience.
# Fix: copy commands literally and run them in a fresh shell to verify before commit.
# File: README.md — update Quickstart, then run `bash` over the snippets.
```

---

## Implementation Blueprint

### Tasks (in order)

```yaml
Task 1 — Integration test infrastructure:
  CREATE tests/integration/__init__.py
  CREATE tests/integration/conftest.py:
    - tmp_log_dir: real Recorder writing to tmp_path
    - mock_keyring: in-memory dict patched into keyring module
    - mock_httpx: respx or MockTransport with predictable responses
    - mock_browser: webbrowser.open returns True without side effect
    - example_config: writes a temp api_configs.json with one mock REST + one GraphQL

Task 2 — Happy path integration test:
  CREATE tests/integration/test_full_flow.py:
    - test_fetch_data_happy_path:
        * Pre-populate mock keyring with TokenInfo(access_token="test_token_xyz",
          expires_at=time.time()+3600) BEFORE invoking the tool. This sidesteps
          OAuth entirely — Phase 3 unit tests already cover that path.
        * Call fetch_data via ToolContext (constructed from fixtures)
        * Assert response shape, status_code, metadata
        * Read tmp_log_dir/audit/YYYY-MM.jsonl: exactly 1 line, tool="fetch_data",
          result="success"
        * Same for usage and insight
        * Grep all JSONL files: no token/secret/auth values appear non-redacted
        * Assert literal "test_token_xyz" does NOT appear in any JSONL line
        * Assert mock transport saw "Authorization: Bearer test_token_xyz"
    - test_fetch_data_auth_required:
        * Mock keyring empty (no token pre-populated)
        * Mock webbrowser.open with a sentinel that records calls
        * Call fetch_data → expect {"error": {"code": "AUTH_REQUIRED", ...}}
        * Assert webbrowser.open was NOT called (tools don't trigger OAuth on
          AUTH_REQUIRED; that's Phase 3's flow, invoked elsewhere)
        * audit event has result="error" (or "auth_required")

Task 3 — Smoke test:
  CREATE tests/integration/test_smoke.py:
    - test_server_boots_and_exits_cleanly:
        * subprocess.Popen(['python', '-m', 'src.server'],
            env={...MCP_LOG_DIR=tmp_path}, stdout=PIPE, stderr=PIPE)
        * Wait for stderr "ready" line OR sleep 1.5s
        * Send SIGTERM; communicate(timeout=10)
        * Assert returncode in {0, -SIGTERM}; assert stdout == b""
        * Assert stderr contains structured log indicating Recorder stop drained

Task 4 — Example config refinements:
  MODIFY config/api_configs.example.json:
    - Fix redirect_uri in example_rest_api: localhost → 127.0.0.1
    - Expand top-level _comment to mention auth.type values + README troubleshooting
    - Add a third entry "public_no_auth_api" with auth=null (REST, two endpoints)
      demonstrating the no-auth branch
    - Keep ALL existing example_rest_api / example_graphql_api fields and ${VAR}
      placeholders intact (no wholesale rewrite)
  CREATE tests/test_example_config.py (or add to existing test_config.py):
    - Loads config/api_configs.example.json
    - Asserts ApiConfigsRoot(**data) succeeds (no ValidationError)
    - Asserts all auth.client_id/token_env values still use ${...} placeholders (no
      accidental real secrets committed)

Task 5 — README updates:
  MODIFY README.md:
    - Quickstart section — verbatim commands from a fresh checkout
    - Configuring an API — point at api_configs.example.json with brief commentary
    - OAuth setup — what to register at the provider; OAUTH_CALLBACK_PORT note
    - Keyring per OS table (macOS/Linux/Windows + headless Linux notes)
    - Troubleshooting subsection — mirror CLAUDE.md table + the three failure modes
      added in the "What" section above
    - Logging subsection — directory layout; retention; operator-only

Task 6 — Verify Quickstart literally:
  In a clean shell (not the dev checkout):
    git clone <this repo path> /tmp/mcp-fresh && cd /tmp/mcp-fresh
    Run each Quickstart command exactly. Fix README if any command fails.
  (Manual step; not gated by pytest — but mention in PR description that it was done.)
```

---

## Integration Points

```yaml
RECORDER:
  Integration tests use a real Recorder with tmp_path. No mocking — the whole point is
  to verify Recorder + redaction + write semantics in concert.

CONFIG:
  Integration tests write a temp config; example config tested separately for validity.

LOGGING:
  Verified end-to-end: Recorder JSONL output checked for redaction.

ENV VARS:
  Integration tests set MCP_LOG_DIR, MCP_LOG_BUFFER_SIZE=1 (so events flush quickly).

DEPENDENCIES:
  - respx (test-only) — flag in commit if added; alternative: use httpx.MockTransport
    (no new dependency).
```

---

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

### Level 2 — Tests

```bash
pytest tests/integration/ -v
pytest tests/ -v            # full suite (Phases 3–5 + integration + 27 events tests)
```

Required:
- Integration happy path passes
- Integration auth-failure path passes
- Smoke test passes (subprocess boot + SIGTERM drain)
- Example config validates against ApiConfigsRoot

### Level 3 — Quickstart smoke (manual, document in PR)

```bash
git clone . /tmp/mcp-fresh && cd /tmp/mcp-fresh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
cp config/api_configs.example.json config/api_configs.json
timeout 2 python -m src.server 2>/tmp/boot.log; rc=$?
test "$rc" -eq 124      # SIGTERM from timeout — clean
test ! -s /dev/stdout   # no stdout
```

---

## MCP Security Checklist

Phase 6 adds no new code in `src/`, but the integration tests must verify:

- [ ] **No secrets in any captured log file.**
      `grep -riE 'authorization:|bearer |client_secret|access_token|api_key|password' tmp_log_dir/`
      → zero matches with non-`<redacted>` values across audit/debug/usage/insight.
- [ ] **Tool responses contain zero auth fields.** Integration test response is asserted
      not to contain `auth`, `client_id`, `token`, `secret` keys.
- [ ] **Stdout is empty during smoke test.** `assert stdout == b""` after server exit.
- [ ] **OAuth callback bind address verified by Phase 3 unit tests** — integration
      tests skip OAuth entirely by pre-populating the in-memory keyring; no real or
      mock callback server is started during integration.
- [ ] **Example config has no real secrets.** `grep -E 'ghp_|sk-' config/api_configs.example.json`
      → zero matches; placeholders use `${VAR_NAME}` syntax only.
- [ ] **README documents the security model accurately** — no claim that logs are safe
      to share without review; states "operator-only".

---

## Risks

1. **Integration test hits the real network** — a missing fixture or wrong patch path
   means `httpx` connects to `api.github.com` (or wherever the example config points).
   This makes CI flaky and may leak the developer's env tokens.
   *Recovery:* Use `respx` (or `httpx.MockTransport`) at the AsyncClient level, not at
   the URL level. Add a CI check that fails if any test makes a real DNS lookup
   (e.g., `pytest --disable-socket` from `pytest-socket`).

2. **Smoke test races the server** — the subprocess may receive SIGTERM before the
   Recorder background task has started, leading to flaky "Recorder didn't drain" failures.
   *Recovery:* Have the server emit a structured "ready" log line at the end of startup;
   the test waits for that line (with timeout) before sending SIGTERM. Falls back to
   1.5s sleep on platforms where the log read isn't reliable.

3. **README quickstart drifts** — fields in `.env.example` or
   `config/api_configs.example.json` change in later phases, but README copy isn't
   updated; first-run experience breaks silently.
   *Recovery:* The example-config validation test (`tests/test_example_config.py`) catches
   schema drift. For README drift, add a checklist item to PR templates that
   "README Quickstart was run on a fresh checkout for this PR."

---

## Final Checklist

- [ ] `ruff check src/ tests/ --fix` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all green (Phases 3–5 + integration + 27 events tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] README Quickstart reproduced on a fresh clone — noted in PR description
- [ ] `config/api_configs.example.json` validates against `ApiConfigsRoot`
- [ ] No new pip dependencies in production `requirements.txt`
  (test-only deps in `requirements-dev.txt` only, flagged in commit)
- [ ] Acceptance criteria from "Success Criteria" — all checked
