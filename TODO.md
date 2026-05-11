# TODO — Resume after Phase 9.5

> Snapshot 2026-05-11, end of session. Pick up here.
>
> See [`INITIAL.md`](INITIAL.md) for the active feature delta (Phase 10).
> This file tracks operational state — what's wired up where, what was last
> verified, and what's left to do.

---

## ✅ Done in this session (Phases 9.0 → 9.5, plus a 9.6 revert)

- **Phase 9.0 — OAuth Provider** (commit `4b3e976`): RFC 8414/7591/7636/9728
  endpoints, encrypted SQLite store, consent HTML form, opaque tokens, 93 tests.
- **Phase 9.1 — Form-encoded login + fingerprint user_id** (`9b17bf1`): Taximail
  compatibility — `application/x-www-form-urlencoded` login bodies and
  `_api_key_fingerprint` for Service APIs that don't expose a stable user id.
- **Phase 9.2 — Contextvar bridge** (`a108fbd`): per-request `user_id` →
  `service_session` wiring so tool handlers reach the right user's Taximail
  session at call time.
- **Phase 9.3 — CORS + RFC 9728 strict URL** (`54ff960`): pure-ASGI CORS
  middleware, slash-boundary path match, strict-variant protected-resource URL.
- **Phase 9.4 — Keyring-backed session_login** (`9a3f3f4`):
  `KeyringServiceSessionStore` and `scripts/session_login.py` — Claude Desktop
  (STDIO) can now hit `session_login` APIs via natural language.
- **Phase 9.5 — LLM-facing endpoint metadata** (`770a183`): `description` /
  `required_params` / `param_hints` on `EndpointConfig`, surfaced through
  `list_apis`. LLM picks correct filters on first try.
- **Phase 9.6 — Local HTTPS** (`a375230`) added then reverted (`12ec7ce`):
  uvicorn TLS support didn't help — Claude Desktop's Add Custom Connector
  routes through Anthropic's cloud, which rejects all localhost URLs
  regardless of scheme.

**Test count: 432 passing.** All gates clean (`ruff`, `ruff format`, `mypy`,
`pytest`). Working tree on `main` is in sync with `origin/main`.

---

## 🔧 Current state of moving parts

| Component | State |
|-----------|-------|
| `~/Library/Application Support/Claude/claude_desktop_config.json` | `data-gateway` STDIO entry present and proven working |
| `~/.codex/config.toml` | `[mcp_servers.data-gateway-http]` block with inlined Bearer token; works against Phase 8 HTTP transport |
| GitHub OAuth token in keyring | Present (refreshed during this session) |
| Taximail session in keyring | Cleared at end of session (test reset). Re-run `python -m scripts.session_login taximail` to restore. |
| SQLite OAuth Provider DB | Cleared (`data/oauth_provider.db` deleted) — recreated on next HTTP+OAuth boot |
| MCP server subprocesses | None running (verified via `pkill -f src.server`) |
| Ports 8080 / 6274 / 6277 | All free |
| mkcert local CA | Installed during Phase 9.6 experiment — harmless to keep, no longer used by this project |

---

## 🚀 Where to pick up next

### Option A — Start Phase 10 (Production Deploy Recipe)

See [`INITIAL.md`](INITIAL.md) for the full delta. Roughly:

1. `Dockerfile` + `docker-compose.yml`
2. `deploy/Caddyfile` (auto Let's Encrypt)
3. `deploy/systemd/mcp-data-gateway.service` for non-Docker hosts
4. `docs/deploy.md` — 30-minute VPS walkthrough (DigitalOcean target)
5. Verify against Claude Desktop's "Add custom connector" — public HTTPS URL should succeed

```bash
/generate-prp INITIAL.md
# review PRPs/phase10-deploy.md
/execute-prp PRPs/phase10-deploy.md
```

### Option B — Add more Service APIs to `config/api_configs.json`

The current config has GitHub (OAuth) and Taximail (session_login). Adding
more — Notion, Linear, Google Calendar, internal APIs — exercises the
endpoint-metadata + natural-language flow without writing any new code.

### Option C — Quality improvements within Phase 9

- Multi-Service-API consent (operator can register more than one `session_login`
  API; consent form picks which one to log into).
- Endpoint path parameters (`/v2/list/{list_id}/subscribers` rather than the
  current hardcoded `35`).
- Token rotation policy + background sweeper for expired tokens.

### Option D — Polish / docs (this is the small option)

- Expand README's Phase 9 section with the Taximail walkthrough.
- Add screenshots / GIFs of the consent form.
- Record a short demo video.

---

## 📊 Session checkpoints

- **Commits this session**: 4b3e976 → 12ec7ce (8 commits, ~7,200 LOC src/, ~7,900 LOC tests/)
- **Last commit**: `12ec7ce Revert Phase 9.6 (Local HTTPS)`
- **Branch**: `main` (pushed to `origin`)
- **Working tree**: clean

---

## 🔍 Reference (muscle memory)

- Run tests: `.venv/bin/python -m pytest tests/ -q` (432 passing)
- Lint + types: `.venv/bin/python -m ruff check src/ scripts/ tests/ && .venv/bin/python -m mypy src/ scripts/ tests/`
- Smoke (stdio): `.venv/bin/python -m src.server` then Ctrl-C
- Smoke (http + OAuth, local): `MCP_TRANSPORT=http MCP_OAUTH_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") MCP_OAUTH_ISSUER=http://127.0.0.1:8080 .venv/bin/python -m src.server`
- Smoke (full OAuth dance against Taximail): `.venv/bin/python -m scripts.oauth_smoke_test`
- OAuth login (GitHub): `.venv/bin/python -m scripts.oauth_login github [--clear]`
- Session login (Taximail or other session_login API): `.venv/bin/python -m scripts.session_login taximail [--clear]`
- OAuth admin (clients / tokens / sessions): `.venv/bin/python -m scripts.oauth_admin {list-clients,list-tokens,list-sessions,revoke-token}`
- Activity logs: `logs/{audit,debug,usage,insight}/YYYY-MM.jsonl`
- Test playbook: [`docs/testing-user-ux.md`](docs/testing-user-ux.md)
- Five MCP tools: `list_apis`, `fetch_data`, `send_data`, `execute_graphql`, `get_status`
- Keyring service: `mcp-data-gateway`, account = `api_id`
- HTTP endpoint: `POST /mcp` on `127.0.0.1:8080` (default)
- OAuth Provider endpoints: `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource[/mcp]`, `/register`, `/authorize`, `/authorize/consent`, `/token`
