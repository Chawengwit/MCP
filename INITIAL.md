# Feature Request

> One feature per file. Run `/generate-prp INITIAL.md` then review the produced PRP and
> run `/execute-prp PRPs/{...}.md`.
>
> **This file is a DELTA**: it does NOT repeat info already in [docs/plan.md](docs/plan.md)
> or [CLAUDE.md](CLAUDE.md). It captures only what's specific or deeper than the roadmap.

---

## STATUS

**Phases 1–9.5 shipped.** Generic MCP Data Gateway with OAuth 2.0 Authorization
Server, multi-tenant per-user sessions, CORS, STDIO keyring auth, and LLM-facing
endpoint metadata — all proven end-to-end against Claude Desktop (STDIO) and
MCP Inspector (HTTP + OAuth) with a live Taximail-style Service API.

| Phase | PRP | Status |
|-------|-----|--------|
| 3 — Authentication | [PRPs/phase3-auth.md](PRPs/phase3-auth.md) | ✅ Done |
| 4 — API Gateway | [PRPs/phase4-gateway.md](PRPs/phase4-gateway.md) | ✅ Done |
| 5 — Tools & Integration | [PRPs/phase5-tools.md](PRPs/phase5-tools.md) | ✅ Done |
| 6 — Testing & Documentation | [PRPs/phase6-testing-docs.md](PRPs/phase6-testing-docs.md) | ✅ Done |
| **GitHub OAuth integration** | (no PRP — direct edit) | ✅ Done — `scripts/oauth_login.py` |
| 8 — HTTP Transport | [PRPs/phase8-http-transport.md](PRPs/phase8-http-transport.md) | ✅ Done — `src/transport/`, `MCP_TRANSPORT` switch |
| 9 — OAuth Provider | [PRPs/phase9-oauth-provider.md](PRPs/phase9-oauth-provider.md) | ✅ Done — `src/oauth_provider/` (RFC 8414/7591/7636/9728), 93 tests |
| 9.1 — Form-encoded login + api_key fingerprint user_id | (direct edit) | ✅ Done — Taximail compatibility |
| 9.2 — contextvar bridge for per-request session | (direct edit) | ✅ Done — middleware → tool-layer wiring |
| 9.3 — CORS middleware + RFC 9728 strict URL | (direct edit) | ✅ Done — `src/transport/cors.py` |
| 9.4 — Keyring-backed `session_login` for STDIO | (direct edit) | ✅ Done — `scripts/session_login.py` |
| 9.5 — LLM-facing endpoint metadata | (direct edit) | ✅ Done — `description` / `required_params` / `param_hints` |
| 9.6 — Local HTTPS for Claude Desktop Custom Connector | (direct edit) | 🔄 Reverted — Anthropic cloud rejects localhost regardless |
| **10 — Production Deploy Recipe** | (TBD) | 🔵 Next |

**Total test count: 432 passing** (288 baseline + 144 across Phase 9.0–9.5).

Verified test scenarios (see [`docs/testing-user-ux.md`](docs/testing-user-ux.md)):

- ✅ **STDIO + Claude Desktop** — natural-language flow with GitHub *and* Taximail
- ✅ **HTTP + MCP Inspector + OAuth** — full RFC 8414/7591/7636 dance, live Taximail call
- ❌ **Claude Desktop "Add custom connector"** — blocked by Anthropic cloud reachability check on localhost URLs (documented)
- ⏳ **Claude.ai web** — waiting on Phase 10 (public deploy)

---

## NEXT FEATURE — Phase 10: Production Deploy Recipe

### FEATURE

Add the operational scaffolding to deploy this MCP server on a public HTTPS
endpoint reachable from Anthropic's cloud, so the existing OAuth Provider
(Phase 9) becomes usable from:

- **Claude Desktop's "Add custom connector"** UI (blocked on localhost today)
- **Claude.ai web's "Add custom connector"** flow
- Any standard MCP / OAuth 2.0 client on the public internet

This is the bridge between "feature-complete code" (where we are now) and
"actually usable by Claude users" (the production reality).

### AUTH STRATEGY

Unchanged from Phase 9. The OAuth Provider, CORS layer, keyring path, and
Service-API integration all work as-is — they just need to be reachable on
HTTPS at a domain Anthropic's backend can resolve.

### ACCEPTANCE CRITERIA

#### Container image

- [ ] `Dockerfile` builds a minimal Python image (slim base, multi-stage if
      it shaves >50 MB) with the MCP server, deps, and configs baked in.
- [ ] Image runs as a **non-root user** (uid 1000) and uses a read-only
      root filesystem where possible.
- [ ] Image exposes port 8080 (HTTP — TLS terminates at the reverse proxy).
- [ ] `HEALTHCHECK` hits a lightweight endpoint (likely the existing
      `/.well-known/oauth-authorization-server` since it's unauthenticated
      and always available when OAuth is on).
- [ ] All env vars documented in `.env.example` are picked up at runtime.

#### Reverse proxy (Caddy preferred for auto Let's Encrypt)

- [ ] `deploy/Caddyfile` (or `nginx.conf` as alternative) terminating TLS
      with auto-issued Let's Encrypt certs.
- [ ] Proxies `https://<domain>/mcp` and `https://<domain>/.well-known/*`
      to the MCP container.
- [ ] Forwards `X-Forwarded-Proto: https` so the MCP server's banner /
      logs / `MCP_OAUTH_ISSUER` reflect the public scheme.
- [ ] CORS headers set by **the MCP server**, not duplicated at the proxy
      (avoid double-Allow-Origin headers).

#### Composition

- [ ] `docker-compose.yml` brings up `mcp-server` + `caddy` (or nginx)
      with one command (`docker compose up -d`).
- [ ] Volumes for: `data/` (SQLite oauth_provider.db), `logs/`, and
      Caddy's certificate store.
- [ ] An optional `compose.dev.yml` overlay for `MCP_TRANSPORT=http` on
      `127.0.0.1:8080` without TLS (mirrors the current local-dev flow).

#### Systemd alternative (non-Docker)

- [ ] `deploy/systemd/mcp-data-gateway.service` — runs the server under a
      dedicated user, `Restart=on-failure`, `ProtectSystem=strict`, etc.
- [ ] Matching `deploy/systemd/mcp-data-gateway.env` (sourced from `EnvironmentFile=`).

#### Deploy guide

- [ ] `docs/deploy.md` — a 30-minute walkthrough for **one** target
      (DigitalOcean droplet preferred; alternative: Fly.io). Includes:
  - Provisioning the VM
  - Pointing DNS at the IP
  - Pulling the repo / building the image
  - Filling `.env` with production values (generate Fernet key, etc.)
  - `docker compose up`
  - Adding the resulting URL as a custom connector in Claude Desktop /
    Claude.ai web — and confirming the OAuth dance completes
- [ ] README gets a "Production Deployment" section linking to the guide.

#### Logging (12-factor compliance)

- [ ] All logs already go to stderr (Phase 7 invariant); production guide
      shows how to wire that to the container runtime's log driver.
- [ ] Activity logs (`logs/*.jsonl`) optionally shipped to a mounted volume
      or stdout — operator choice, documented.

### EDGE CASES

- **Cert renewal**: Let's Encrypt certs are 90 days. Caddy auto-renews;
  guide notes how to verify.
- **SQLite under concurrent load**: WAL mode handles writes serially.
  Acceptable for typical OAuth volumes (token issuance is bursty-low).
- **Operator key rotation**: `MCP_OAUTH_ENCRYPTION_KEY` rotation
  invalidates all stored sessions. Document the cutover (clear
  `service_sessions` table + force re-consent).
- **Reverse-proxy bypass**: the MCP server should **only** bind to
  `127.0.0.1` (or a docker network), never `0.0.0.0`. The proxy is the
  only public entry point.

### OUT OF SCOPE

- Kubernetes manifests / Helm charts — overkill for a single-instance MCP.
- Metrics / observability tooling (Prometheus, Grafana, OpenTelemetry).
- Multi-region / HA deploys.
- Database migrations beyond the existing SQLite DDL.
- A web admin UI — the `scripts/oauth_admin.py` CLI remains the operator
  surface.

### REFERENCE PATTERNS

- **Docker base image**: `python:3.14-slim` for size; pin to a specific
  patch in production.
- **Caddyfile**: minimal, JSON config not required.
- **12-factor logging**: stderr only, no file rotation inside the container
  (let the runtime / journald handle it).

### NEW DEPENDENCIES

None at the Python layer. New artifacts are operational (Dockerfile,
compose, Caddyfile, systemd unit, docs).

### VALIDATION GATES

```bash
ruff check src/ scripts/ tests/ --fix
ruff format src/ scripts/ tests/
mypy src/ scripts/ tests/
pytest tests/ -v          # baseline 432; Phase 10 may add a smoke test
```

Plus operational gates (manual):

- `docker compose up` succeeds, container is healthy
- `curl https://<domain>/.well-known/oauth-authorization-server` returns 200 with the expected issuer URL
- Adding the connector in Claude Desktop succeeds (no 400 "localhost" error)
- The OAuth consent flow completes; a real tool call returns Service API data

### EXPECTED DELIVERABLES

- `Dockerfile`
- `docker-compose.yml`
- `deploy/Caddyfile`
- `deploy/systemd/mcp-data-gateway.service`
- `deploy/systemd/mcp-data-gateway.env.example`
- `docs/deploy.md`
- `README.md` — new "Production Deployment" subsection
- `.dockerignore`
- (Optional) `compose.dev.yml` for local-dev parity

---

## ARCHIVE — completed features (use git history for the full deltas)

- **Phase 8 — HTTP transport** (`1dafca7`): `MCP_TRANSPORT={stdio,http}`,
  Streamable HTTP via uvicorn + Starlette, Bearer-token middleware,
  loopback safety guard, 41 new tests (288 total at that point).
- **Phase 9.0 — OAuth Provider** (`4b3e976`): RFC 8414 discovery,
  RFC 7591 dynamic registration, RFC 7636 PKCE S256, encrypted SQLite
  store, consent HTML form, 93 new tests.
- **Phase 9.1 — Form-encoded login + fingerprint user_id** (`9b17bf1`):
  `auth.login_content_type = "application/x-www-form-urlencoded"` and
  `user_id_field = "_api_key_fingerprint"` for Service APIs (like
  Taximail) that don't expose a stable per-user identifier.
- **Phase 9.2 — Contextvar bridge** (`a108fbd`): publishes the OAuth
  middleware's resolved user_id to a `contextvars.ContextVar` so tool
  handlers running deeper in the MCP SDK can pick up the right user's
  Service API session.
- **Phase 9.3 — CORS + RFC 9728 strict URL variant** (`54ff960`): pure-ASGI
  CORS middleware (outermost), preflight handling, configurable
  `MCP_CORS_ALLOWED_ORIGINS`. Discovery accepts both
  `/.well-known/oauth-protected-resource` and the strict
  `/.well-known/oauth-protected-resource/mcp`.
- **Phase 9.4 — Keyring-backed session_login for STDIO** (`9a3f3f4`):
  `KeyringServiceSessionStore` and `scripts/session_login.py` — operators
  log in once via terminal, then natural-language flow in Claude
  Desktop reaches Service APIs (Taximail, …).
- **Phase 9.5 — LLM-facing endpoint metadata** (`770a183`):
  `description` / `required_params` / `param_hints` on `EndpointConfig`,
  surfaced through `list_apis`. The LLM now fills required filters on
  the first try instead of probing the upstream API.
- **Phase 9.6 — Local HTTPS** (`a375230`, reverted in `12ec7ce`):
  uvicorn TLS support was added to make Claude Desktop's Custom
  Connector dialog accept the URL. Real-world testing showed the
  dialog routes through Anthropic's cloud API, which rejects all
  localhost URLs regardless of protocol — so the TLS code added
  maintenance surface for zero benefit and was removed. TLS will
  return at the reverse-proxy layer in Phase 10.
