"""Schema-drift guard for `config/api_configs.example.json`.

The example config is the only template a new user has — if it stops validating
against `ApiConfigsRoot`, first-run breaks silently. These tests catch:

  - Pydantic schema changes that the example wasn't updated for.
  - Accidental literal secrets sneaking into a committed file.
  - Documented auth.type values drifting from what `auth_resolver` recognizes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from src.config import ApiConfigsRoot
from src.tools.auth_resolver import KNOWN_AUTH_TYPES

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "config" / "api_configs.example.json"


def _load_raw() -> dict[str, Any]:
    return json.loads(EXAMPLE_PATH.read_text())


def _load_with_placeholder_substitution(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Set every ${VAR} referenced in the example to a benign value, then load.

    `load_api_configs` substitutes env vars and raises on unresolved placeholders.
    Tests that exercise the loader need to ensure all referenced vars are set.
    """
    raw = EXAMPLE_PATH.read_text()
    for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw):
        monkeypatch.setenv(var, f"test-value-for-{var}")
    return json.loads(raw)


def test_example_config_validates_against_apiconfigs_root() -> None:
    """The shipped example must round-trip through ApiConfigsRoot without errors."""
    raw = _load_raw()
    # Substitute placeholders with dummy values for Pydantic — the schema doesn't
    # validate URL substring content, only structural shape.
    root = ApiConfigsRoot(**raw)
    assert len(root.apis) >= 1
    for api_id, cfg in root.apis.items():
        assert cfg.base_url.startswith(("http://", "https://")), api_id
        assert cfg.type in {"rest", "graphql"}, api_id


def test_example_config_loader_resolves_with_placeholders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: write the example to disk, set placeholders, run load_api_configs."""
    from src.config import load_api_configs

    target = tmp_path / "api_configs.json"
    raw_text = EXAMPLE_PATH.read_text()
    for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw_text):
        monkeypatch.setenv(var, f"test-value-for-{var}")
    target.write_text(raw_text)

    configs = load_api_configs(target)
    assert configs, "load_api_configs returned an empty dict"


def test_example_config_uses_only_known_auth_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `auth.type` in the example must be one auth_resolver actually handles."""
    raw = _load_with_placeholder_substitution(monkeypatch)
    root = ApiConfigsRoot(**raw)
    for api_id, cfg in root.apis.items():
        if cfg.auth is None:
            continue
        assert cfg.auth.type in KNOWN_AUTH_TYPES, (
            f"{api_id} uses auth.type={cfg.auth.type!r}, not in KNOWN_AUTH_TYPES "
            f"({sorted(KNOWN_AUTH_TYPES)}). Update auth_resolver or fix the example."
        )


def test_example_config_has_no_committed_secrets() -> None:
    """Catch literal secrets sneaking in.

    Allow only `${VAR}` placeholders for credential-bearing fields. Real-looking
    tokens (GitHub `ghp_*`, OpenAI `sk-*`, Slack `xoxb-*`, AWS `AKIA*`) must
    NEVER appear in the committed file.
    """
    raw_text = EXAMPLE_PATH.read_text()
    forbidden_prefixes = ("ghp_", "ghs_", "sk-", "xoxb-", "xoxp-", "AKIA")
    for prefix in forbidden_prefixes:
        assert prefix not in raw_text, (
            f"Suspicious literal token prefix {prefix!r} found in committed example. "
            f"Replace with a ${{ENV_VAR}} placeholder."
        )


def test_example_config_credential_fields_use_placeholders() -> None:
    """Specifically: client_id / client_secret / token_env / key_env values must
    either be `${VAR}` placeholders OR (for *_env fields) plain env-var names.

    A literal value like `client_secret: "abc123"` would be a committed secret.
    """
    raw = _load_raw()
    placeholder = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
    env_var_name = re.compile(r"^[A-Z_][A-Z0-9_]*$")

    for api_id, cfg in raw["apis"].items():
        auth = cfg.get("auth") if isinstance(cfg, dict) else None
        if auth is None:
            continue
        # Direct-value secrets — must be a ${VAR} placeholder.
        for field in ("client_id", "client_secret"):
            value = auth.get(field)
            if value is None:
                continue
            assert placeholder.match(value), (
                f"{api_id}.auth.{field} is not a ${{VAR}} placeholder: {value!r}"
            )
        # Indirect-secret pointers — env var NAMES live here, not values.
        for field in ("token_env", "key_env"):
            value = auth.get(field)
            if value is None:
                continue
            assert env_var_name.match(value), (
                f"{api_id}.auth.{field} should be a bare ENV_VAR name, "
                f"got {value!r} — looks like a literal secret value."
            )


def test_example_config_callback_uses_loopback_ip() -> None:
    """Phase 3 binds the callback server to 127.0.0.1; the example must match."""
    raw = _load_raw()
    for api_id, cfg in raw["apis"].items():
        auth = cfg.get("auth") if isinstance(cfg, dict) else None
        if auth is None:
            continue
        redirect_uri = auth.get("redirect_uri")
        if redirect_uri is None:
            continue
        # Must use 127.0.0.1, not localhost (browsers may treat them as different
        # origins for OAuth state tracking — see CLAUDE.md security rules).
        assert "localhost" not in redirect_uri, (
            f"{api_id}.auth.redirect_uri uses 'localhost'; standardize on 127.0.0.1"
        )
        assert "127.0.0.1" in redirect_uri or redirect_uri.startswith("https://"), (
            f"{api_id}.auth.redirect_uri must use 127.0.0.1 (loopback) for local "
            f"callbacks: got {redirect_uri!r}"
        )


def test_dotenv_example_has_no_committed_secrets() -> None:
    """`.env.example` should also use placeholders or empty values, never literals."""
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    if not env_example.exists():
        pytest.skip(".env.example not present — skipping placeholder check")
    text = env_example.read_text()
    forbidden_prefixes = ("ghp_", "ghs_", "sk-", "xoxb-", "xoxp-", "AKIA")
    for prefix in forbidden_prefixes:
        assert prefix not in text, f".env.example contains a literal token starting with {prefix!r}"
