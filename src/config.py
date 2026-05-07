from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ApiAuthConfig(BaseModel):
    type: str
    provider: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # populated via ${VAR} substitution at load time
    authorize_url: str | None = None
    token_url: str | None = None
    redirect_uri: str | None = None
    scopes: list[str] | None = None
    token_env: str | None = None
    header_name: str | None = None
    key_env: str | None = None


class EndpointConfig(BaseModel):
    method: str | None = None
    path: str | None = None
    operation: str | None = None
    query: str | None = None
    query_params: list[str] | None = None
    body_schema: str | None = None


class ApiLoggingConfig(BaseModel):
    request_payload: str = "metadata"
    response_payload: str = "summary"
    redact_fields: list[str] = Field(default_factory=list)


class ApiLimitsConfig(BaseModel):
    timeout_seconds: int = 30
    max_retries: int = 3


class ApiConfig(BaseModel):
    type: str
    base_url: str
    auth: ApiAuthConfig | None = None
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    logging: ApiLoggingConfig | None = None
    limits: ApiLimitsConfig | None = None


class ApiConfigsRoot(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Accept _comment and other top-level fields

    apis: dict[str, ApiConfig]


def load_api_configs(path: Path | None = None) -> dict[str, ApiConfig]:
    """Load and parse API configurations from JSON file.

    Args:
        path: Path to config/api_configs.json. If None, defaults to ./config/api_configs.json

    Returns:
        dict mapping API name to ApiConfig. Empty dict if file is missing
        (a warning is emitted to stderr in that case).

    Raises:
        json.JSONDecodeError: If JSON is invalid
        ValueError: If ${VAR} placeholder is unresolved or config structure is invalid
    """
    if path is None:
        path = Path("config/api_configs.json")

    if not path.exists():
        print(
            f"[mcp.config] Warning: config file not found at {path}. "
            "Using empty API configuration.",
            file=sys.stderr,
        )
        return {}

    # Load JSON
    try:
        with open(path) as f:
            raw_config = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in {path}: {e.msg}",
            e.doc,
            e.pos,
        ) from e

    # Perform ${VAR} substitution (single-pass)
    substituted_config = _substitute_env_vars(raw_config)

    # Validate with Pydantic
    try:
        root = ApiConfigsRoot(**substituted_config)
    except ValidationError as e:
        raise ValueError(f"Invalid API config structure: {e}") from e

    return root.apis


def _substitute_env_vars(obj: Any) -> Any:
    """Recursively substitute ${VAR_NAME} placeholders with environment values.

    Single-pass: substituted values are NOT re-scanned, preventing recursive expansion
    and user data being interpreted as templates.

    Args:
        obj: Any JSON-serializable object (dict, list, str, etc.)

    Returns:
        Object with all ${VAR_NAME} placeholders replaced

    Raises:
        ValueError: If a placeholder ${VAR} cannot be resolved from environment
    """
    if isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_substitute_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        return _substitute_string(obj)
    else:
        return obj


def _substitute_string(s: str) -> str:
    """Replace ${VAR_NAME} with environment variable values (single-pass).

    Single-pass: substituted values are NOT re-scanned. Prevents user-supplied
    env values from being interpreted as templates. Regression-guarded by
    tests/test_config.py::test_load_api_configs_single_pass_substitution.
    """

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(
                f"Unresolved environment variable: ${{{var_name}}}. "
                f"Set {var_name} in .env or environment."
            )
        return value

    # re.sub applies replacer once per match against the *original* string —
    # the substituted result is never re-scanned. Do not switch to a recursive
    # implementation.
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replacer, s)
