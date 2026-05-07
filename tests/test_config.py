from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from src.config import load_api_configs


def test_load_api_configs_valid() -> None:
    """Test loading a valid config file."""
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "apis": {
                "example_api": {
                    "type": "rest",
                    "base_url": "https://api.example.com",
                    "endpoints": {"list_users": {"method": "GET", "path": "/users"}},
                }
            }
        }
        config_path.write_text(json.dumps(config_data))

        result = load_api_configs(config_path)

        assert "example_api" in result
        assert result["example_api"].type == "rest"
        assert result["example_api"].base_url == "https://api.example.com"


def test_load_api_configs_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that missing file returns empty dict with warning."""
    missing_path = Path("/nonexistent/api_configs.json")

    result = load_api_configs(missing_path)

    assert result == {}
    captured = capsys.readouterr()
    assert "Warning: config file not found" in captured.err


def test_load_api_configs_invalid_json() -> None:
    """Test that invalid JSON raises JSONDecodeError."""
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_path.write_text("{invalid json}")

        with pytest.raises(json.JSONDecodeError):
            load_api_configs(config_path)


def test_load_api_configs_with_comment_field() -> None:
    """Test that _comment field is accepted and ignored."""
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "_comment": "This is a comment",
            "apis": {
                "example_api": {
                    "type": "rest",
                    "base_url": "https://api.example.com",
                    "endpoints": {},
                }
            },
        }
        config_path.write_text(json.dumps(config_data))

        result = load_api_configs(config_path)

        assert "example_api" in result
        assert "_comment" not in result


def test_load_api_configs_env_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that ${VAR} placeholders are substituted from environment."""
    monkeypatch.setenv("EXAMPLE_URL", "https://real-api.example.com")

    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "apis": {
                "example_api": {
                    "type": "rest",
                    "base_url": "${EXAMPLE_URL}",
                    "endpoints": {},
                }
            }
        }
        config_path.write_text(json.dumps(config_data))

        result = load_api_configs(config_path)

        assert result["example_api"].base_url == "https://real-api.example.com"


def test_load_api_configs_unresolved_var() -> None:
    """Test that unresolved ${VAR} raises ValueError."""
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "apis": {
                "example_api": {
                    "type": "rest",
                    "base_url": "${MISSING_VAR}",
                    "endpoints": {},
                }
            }
        }
        config_path.write_text(json.dumps(config_data))

        with pytest.raises(ValueError, match="Unresolved environment variable"):
            load_api_configs(config_path)


def test_load_api_configs_single_pass_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that substitution is single-pass (no re-scanning of substituted values)."""
    monkeypatch.setenv("VAR_A", "${VAR_B}")
    monkeypatch.setenv("VAR_B", "final_value")

    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "apis": {
                "example_api": {
                    "type": "rest",
                    "base_url": "${VAR_A}",
                    "endpoints": {},
                }
            }
        }
        config_path.write_text(json.dumps(config_data))

        result = load_api_configs(config_path)

        # Should resolve to the literal string "${VAR_B}", not "final_value"
        assert result["example_api"].base_url == "${VAR_B}"


def test_load_api_configs_nested_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test ${VAR} substitution in nested structures."""
    monkeypatch.setenv("API_TYPE", "graphql")
    monkeypatch.setenv("API_URL", "https://graphql.example.com")

    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "apis": {
                "gql_api": {
                    "type": "${API_TYPE}",
                    "base_url": "${API_URL}",
                    "endpoints": {},
                }
            }
        }
        config_path.write_text(json.dumps(config_data))

        result = load_api_configs(config_path)

        assert result["gql_api"].type == "graphql"
        assert result["gql_api"].base_url == "https://graphql.example.com"
