-- OAuth Provider schema for the MCP Data Gateway (Phase 9).
--
-- All tables use TEXT keys (opaque tokens / generated client_ids) and
-- INTEGER UNIX-epoch timestamps for portability. Datetime values land in
-- this file as REAL/INTEGER seconds because SQLite has no native datetime
-- type — the application layer (src/oauth_provider/store.py) converts.
--
-- Pragmas are set per-connection by store.py (they are not persistent in
-- the file): foreign_keys=ON, journal_mode=WAL.

CREATE TABLE IF NOT EXISTS clients (
    client_id     TEXT PRIMARY KEY,
    client_name   TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,            -- JSON-encoded list[str]
    created_at    INTEGER NOT NULL          -- epoch seconds UTC
);

CREATE TABLE IF NOT EXISTS authorization_codes (
    code                  TEXT PRIMARY KEY,
    client_id             TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    user_id               TEXT NOT NULL,
    redirect_uri          TEXT NOT NULL,
    code_challenge        TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL CHECK (code_challenge_method = 'S256'),
    expires_at            INTEGER NOT NULL,  -- epoch seconds UTC
    created_at            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_authorization_codes_expires_at
    ON authorization_codes(expires_at);

CREATE TABLE IF NOT EXISTS access_tokens (
    token         TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    refresh_token TEXT UNIQUE,                 -- nullable; UNIQUE so lookups are O(log n)
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_tokens_refresh_token
    ON access_tokens(refresh_token);

CREATE INDEX IF NOT EXISTS idx_access_tokens_user_id
    ON access_tokens(user_id);

CREATE TABLE IF NOT EXISTS service_sessions (
    user_id              TEXT PRIMARY KEY,
    company_group        TEXT,
    encrypted_api_key    BLOB NOT NULL,
    encrypted_secret_key BLOB NOT NULL,
    session_id           TEXT NOT NULL,
    session_expire       INTEGER NOT NULL,    -- epoch seconds UTC
    user_type            TEXT,
    app_package          TEXT,
    updated_at           INTEGER NOT NULL
);
