# Testing User UX — STDIO & HTTP+OAuth

End-to-end test playbook for validating the MCP Data Gateway from a **real user's
perspective**: minimal terminal use, browser-based credential entry where possible,
"deployed-like" operator workflow.

Two scenarios cover the two supported transports:

- **Scenario A — STDIO (Claude Desktop)** — single operator on local machine.
  Credentials stored in OS keyring via a one-time CLI.
- **Scenario B — HTTP + OAuth (MCP Inspector or Claude.ai web)** — multi-user via
  browser. Credentials entered through the consent form, stored encrypted in SQLite.

> 💡 The `Operator` is the person who deploys the server.
> The `User` is the person chatting with Claude.
> In a real production setup these are different people. While developing, they
> are the same person — but the test still separates "hats" so you can feel each
> role's UX.

---

## Prerequisites

Both scenarios assume:

- Working `.venv/` with all dependencies installed (`pip install -r requirements.txt`).
- `config/api_configs.json` includes a `taximail` (or equivalent) entry with
  `auth.type = session_login`.
- Operator has a valid Taximail (or equivalent Service API) `api_key` + `secret_key`
  to test with.

### Optional baseline reset

If you've run tests earlier in the day, clean state before starting:

```bash
# Kill MCP server subprocesses spawned by Claude Desktop (or anything else)
pkill -f "src.server" 2>/dev/null

# Clear the keyring entry (STDIO mode storage)
.venv/bin/python -m scripts.session_login taximail --clear

# Clear the OAuth Provider SQLite (HTTP mode storage)
rm -f data/oauth_provider.db data/oauth_provider.db-shm data/oauth_provider.db-wal

# Confirm clean state — run each line separately
.venv/bin/python -c "import keyring; print(keyring.get_password('mcp-data-gateway', 'taximail'))"
ls data/oauth_provider.db 2>/dev/null || echo "no SQLite"
ps aux | grep -E "src.server" | grep -v grep
lsof -ti:8080,6274,6277 || echo "free"
```

Expected output:

- `None`
- `no SQLite`
- (empty — no MCP processes)
- `free`

---

## Scenario A — STDIO (Claude Desktop)

### Phase 1 — Operator setup (text editor only — no terminal)

- [ ] **A1.** Open `~/Library/Application Support/Claude/claude_desktop_config.json`
- [ ] **A2.** Confirm the `data-gateway` block is present:
  ```json
  {
    "mcpServers": {
      "data-gateway": {
        "command": "/Users/<you>/Documents/MCP/.venv/bin/python",
        "args": ["/Users/<you>/Documents/MCP/src/server.py"]
      }
    }
  }
  ```
- [ ] **A3.** Save the file

### Phase 2 — User: first try (expect AUTH_REQUIRED)

- [ ] **A4.** Launch Claude Desktop (Spotlight → "Claude" → Enter)
- [ ] **A5.** Open a new conversation
- [ ] **A6.** Send the prompt:
  > **"Get me 3 Taximail subscribers"**
- [ ] **A7.** Expected response — Claude reports it can't authenticate yet and
      asks you to run:
  ```
  python -m scripts.session_login taximail
  ```

> 🛑 If Claude returns real data here, something cached. Kill subprocesses
> (`pkill -f "src.server"`) and try again — see "Optional baseline reset" above.

### Phase 3 — Operator: one-time login (terminal)

- [ ] **A8.** Open a terminal at the project root:
  ```bash
  cd /Users/<you>/Documents/MCP
  ```
- [ ] **A9.** Run the login CLI:
  ```bash
  .venv/bin/python -m scripts.session_login taximail
  ```
- [ ] **A10.** When prompted, paste your credentials:
  - `taximail api_key:` — paste the api_key (visible)
  - `taximail secret_key (hidden):` — paste the secret_key (hidden — typing
    does not show on screen)
- [ ] **A11.** Expected output:
  ```
  Authenticating with taximail...
  ✓ Stored session for 'taximail'
    user_id      = taximail:<fingerprint>
    company      = taximail
    user_type    = user
    app_package  = free
  ```

### Phase 4 — User: production use (no terminal)

- [ ] **A12.** Quit Claude Desktop completely (`Cmd+Q`)
- [ ] **A13.** Launch Claude Desktop again — this re-spawns the MCP server
      subprocess with the fresh keyring entry visible
- [ ] **A14.** Re-send the prompt:
  > **"Get me 3 Taximail subscribers"**
- [ ] **A15.** Expected response — Claude returns real Taximail data
      (`subscriber_count` etc.)
- [ ] **A16.** Try variations to feel the natural-language UX:
  - *"List my Taximail subscribers, just the first 5"*
  - *"What's the auth status of my APIs?"*
  - *"Show subscribers from list 35 sorted by email"*

### Phase 5 — Daily use

- [ ] **A17.** Just chat. No terminal needed.
- [ ] **A18.** Session refreshes silently when < 60 s from expiry — the keyring
      holds `api_key` + `secret_key`, so the gateway re-authenticates
      automatically without user involvement.
- [ ] **A19.** Re-login only required when the operator rotates credentials in
      the Service API's own dashboard:
  ```bash
  .venv/bin/python -m scripts.session_login taximail --clear
  .venv/bin/python -m scripts.session_login taximail
  ```

### STDIO key takeaways

- ❌ **No in-app login UI** — STDIO has no HTML / form surface.
- 🟡 **User opens a terminal exactly once** — Phase 3.
- ✅ **After Phase 3, everything is conversational** — no further terminal use.
- ✅ **Token refresh is invisible** — the operator never sees a re-auth prompt
      until credentials are revoked at the Service API.

---

## Scenario B — HTTP + OAuth (MCP Inspector or Claude.ai web)

### Phase 1 — Operator deploy (terminal, then leave running)

This phase simulates a production deploy — the operator boots the server and
forgets it. Leave the terminals running for the entire test.

- [ ] **B1.** Terminal #1 — generate a Fernet key for at-rest encryption:
  ```bash
  cd /Users/<you>/Documents/MCP
  export MCP_OAUTH_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  echo "key set: ${MCP_OAUTH_ENCRYPTION_KEY:0:8}..."
  ```
- [ ] **B2.** Terminal #1 — start the MCP server with the OAuth Provider
      enabled:
  ```bash
  MCP_TRANSPORT=http \
  MCP_OAUTH_ISSUER="http://127.0.0.1:8080" \
  .venv/bin/python -m src.server
  ```
  Expected:
  ```
  [...] HTTP transport listening on http://127.0.0.1:8080/mcp (bearer=OAuth Provider)
  ```
  > Leave this terminal running. Do not interact with it.

- [ ] **B3.** Terminal #2 — launch MCP Inspector (browser-based MCP test client):
  ```bash
  npx @modelcontextprotocol/inspector
  ```
  Copy the URL it prints — looks like
  `http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=<...>`.
  > Leave this terminal running too.

### Phase 2 — User: connect (browser only — no terminal)

- [ ] **B4.** Open the Inspector URL from B3 in a browser
- [ ] **B5.** In the Inspector UI sidebar, configure the connection:
  - **Transport Type**: `Streamable HTTP`
  - **URL**: `http://127.0.0.1:8080/mcp`
  - **Authentication**: `OAuth`
- [ ] **B6.** Click **Connect**
- [ ] **B7.** A new browser tab opens automatically — this is the consent form
      served by our MCP server:
  ```
  Authorize MCP Inspector
  Sign in with your Service API credentials.

  API key:    [_________________]
  Secret key: [_________________]
  [ Authorize ]
  ```
- [ ] **B8.** Paste credentials **in the form** (not in any terminal):
  - **API key** field — paste api_key
  - **Secret key** field — paste secret_key
- [ ] **B9.** Click **Authorize**
- [ ] **B10.** Watch the browser flow complete automatically:
  - `POST /authorize/consent` → 302 (auth code generated)
  - Redirect to Inspector callback (`http://localhost:6274/oauth/callback?code=...`)
  - Inspector → `POST /token` → access_token issued
  - Inspector reconnects to `/mcp` with the new Bearer token
- [ ] **B11.** Inspector status indicator should be green / "Connected"

### Phase 3 — User: call tools (browser only)

- [ ] **B12.** Click the **Tools** tab — five tools should appear:
      `list_apis`, `fetch_data`, `send_data`, `execute_graphql`, `get_status`
- [ ] **B13.** Test `list_apis` first (no arguments):
  - Click `list_apis` → **Run Tool**
  - Expected: both `github` and `taximail` listed with their endpoint metadata
- [ ] **B14.** Test `fetch_data` against Taximail (the real flow):
  - Click `fetch_data`
  - Fill the fields:
    - `api_id`: `taximail`
    - `endpoint`: `list_subscribers`
    - `filters` (JSON): `{"display_mode": "all", "limit": 3}`
  - Click **Run Tool**
- [ ] **B15.** Expected: a real Taximail response, e.g.
  ```json
  {
    "data": {
      "status": "success",
      "data": { "subscriber_count": 0, "list_subscriber": [], ... }
    },
    "metadata": { "source": "taximail", "duration_ms": ... }
  }
  ```

### Phase 4 — Verify (operator hat, terminal #3)

Optional sanity check from a third terminal:

- [ ] **B16.** List registered OAuth clients:
  ```bash
  .venv/bin/python -m scripts.oauth_admin list-clients
  ```
  Expected: 1 client named `MCP Inspector` with the registered redirect URIs.
- [ ] **B17.** List service sessions (per user, encrypted):
  ```bash
  .venv/bin/python -m scripts.oauth_admin list-sessions
  ```
  Expected: one row with the api_key fingerprint as `user_id`, `session_id`
  masked.
- [ ] **B18.** List access tokens:
  ```bash
  .venv/bin/python -m scripts.oauth_admin list-tokens
  ```
  Expected: one token issued to the Inspector client, masked.

### Phase 5 — Ongoing use

- [ ] **B19.** Closing the browser and re-opening Inspector later — the token
      is still in SQLite, so re-connecting picks it up.
- [ ] **B20.** When the Service API session is < 60 s from expiry, the gateway
      transparently re-authenticates using the encrypted credentials.
- [ ] **B21.** Operator can revoke a token without restarting anything:
  ```bash
  .venv/bin/python -m scripts.oauth_admin revoke-token <token-prefix>
  ```

### Claude.ai web equivalent

The browser-side flow (B4-B15) is **exactly the same** for Claude.ai's "Add
custom connector" feature — Inspector is just a developer-friendly stand-in.
The only differences:

- The MCP server URL must be a public HTTPS endpoint Claude.ai can reach
  (ngrok / Cloudflare Tunnel / VPS deploy).
- The user adds the connector from Claude.ai's Settings → Connectors →
  "Add custom connector".
- The consent form is the same HTML page, served by the same `/authorize`
  endpoint of the MCP server.

### HTTP + OAuth key takeaways

- ✅ **In-browser login form** — no terminal at all for the user.
- ✅ **Multi-user ready** — each user has their own access token and
      encrypted service session.
- ✅ **Standards-compliant** — Claude.ai, Inspector, or any RFC 8414 / 7591 /
      7636 OAuth client works without server-side changes.
- 🟡 **Operator runs the server** — terminal time for them, none for users.

---

## Why there's no "Claude Desktop Custom Connector" scenario

Claude Desktop's **Settings → Connectors → Add custom connector** dialog
looks like an obvious third scenario, but it routes the URL through
Anthropic's cloud API for validation. Observed error when adding
`https://127.0.0.1:8080/mcp`:

```
POST /api/organizations/{orgId}/mcp/remote_servers
400 invalid_request_error
"url: Localhost URLs cannot be used because our servers cannot reach
your local machine. Provide a publicly accessible MCP server URL."
```

Anthropic's backend must reach the MCP URL itself, so any `127.0.0.1` /
`localhost` / private-network address is rejected. HTTPS, valid certs,
and CORS make no difference — the constraint is network reachability
from Anthropic's cloud, not protocol.

Practical consequences:

- ✅ **For local development**, use Scenario A (STDIO + Claude Desktop)
  or Scenario B (HTTP + Inspector). Both cover the natural-language UX
  without leaving the operator's machine.
- ✅ **For real production**, deploy the MCP server to a public HTTPS
  endpoint (VPS, Railway, Fly.io, etc.) and add it as a custom
  connector from there. The OAuth Provider + CORS layers from Phase 9
  are already production-ready; only the deployment + DNS + real-
  domain TLS steps remain (tracked as Phase 10).
- 🚫 **Cloudflare Tunnel / ngrok** would technically work but route
  traffic through a third party — verify against your company's
  policy before using.

---

## Side-by-side comparison

| Aspect | Scenario A (STDIO) | Scenario B (HTTP + OAuth) |
|---|---|---|
| Operator setup | text-editor edit `claude_desktop_config.json` | start MCP server + Inspector in two terminals |
| User opens | Claude Desktop app | Browser → Inspector / Claude.ai |
| **Where the user types credentials** | **Terminal CLI prompt** (`session_login`) | **HTML form in browser** (consent page) |
| Credential storage | OS keyring (one entry per api_id) | Encrypted SQLite (one row per user) |
| Subprocess refresh | Quit + relaunch Claude Desktop | Automatic via OAuth flow |
| Multi-user support | ❌ single operator | ✅ many users |
| Token refresh on expiry | ✅ automatic from keyring | ✅ automatic from SQLite |
| Production audience | Self-hosted local dev | Shared / hosted deployment |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Scenario A: prompt returns real data before login | Stale MCP subprocess cached old session | `pkill -f "src.server"` + quit Claude Desktop + reopen |
| Scenario A: AUTH_REQUIRED even after `session_login` | Subprocess started before login | Cmd+Q Claude Desktop, then relaunch |
| Scenario B: consent form 500 | Server hit Service API and got a server error | Check Terminal #1 server stderr for traceback |
| Scenario B: Inspector "OAuth Authorization Error" | Stale Inspector proxy on port 6277 | `lsof -ti:6277 \| xargs kill -9` + restart Inspector |
| Scenario B: CORS errors in browser DevTools | `Origin` not in allowlist | Add origin to `MCP_CORS_ALLOWED_ORIGINS` env var |
| Service API call fails after consent | Service API rejected credentials | Verify credentials in Taximail dashboard, re-run consent |

---

## What this playbook proves

Running both scenarios end-to-end exercises every piece of Phase 9:

- Phase 9.0 — OAuth Authorization Server endpoints (discovery, register,
  authorize, consent, token) — used in Scenario B
- Phase 9.1 — form-encoded login + api_key fingerprint user_id — used
  whenever the Service API is reached, in both scenarios
- Phase 9.2 — contextvar bridge from middleware to tool — exercised by
  Scenario B tool calls
- Phase 9.3 — CORS middleware + RFC 9728 strict URL — exercised by Scenario B
  browser flow
- Phase 9.4 — keyring-backed STDIO session store — exercised by Scenario A
- Phase 9.5 — LLM-facing endpoint metadata — observable when Claude calls
  the right tool/filters on the first try in Scenario A's natural-language
  prompts

If both scenarios pass, the gateway is end-to-end production-ready for
single-operator STDIO use **and** multi-user HTTP+OAuth use.
