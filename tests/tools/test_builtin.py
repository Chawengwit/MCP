from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from src.config import (
    ApiAuthConfig,
    ApiConfig,
    ApiLimitsConfig,
    ApiLoggingConfig,
    EndpointConfig,
)
from src.events import Recorder
from src.tools import list_apis


async def test_list_apis_returns_correct_shape(recorder: Recorder) -> None:
    """list_apis returns the standard data + metadata response shape."""
    api_configs = {
        "example_rest_api": ApiConfig(
            type="rest",
            base_url="https://api.example.com",
            endpoints={
                "list_users": EndpointConfig(),
                "get_user": EndpointConfig(),
            },
        ),
        "example_gql_api": ApiConfig(
            type="graphql",
            base_url="https://graphql.example.com",
            endpoints={"viewer": EndpointConfig()},
        ),
    }

    result = await list_apis(uuid4(), recorder, api_configs)

    assert "data" in result
    assert "metadata" in result
    assert isinstance(result["data"], list)
    assert len(result["data"]) == 2

    api_entry = result["data"][0]
    assert set(api_entry.keys()) == {"name", "type", "base_url", "endpoints"}


async def test_list_apis_omits_auth_logging_limits(recorder: Recorder) -> None:
    """list_apis MUST NOT leak auth/logging/limits fields to the client."""
    api_configs = {
        "secure_api": ApiConfig(
            type="rest",
            base_url="https://secure.example.com",
            auth=ApiAuthConfig(type="oauth2", provider="google"),
            logging=ApiLoggingConfig(request_payload="summary"),
            limits=ApiLimitsConfig(timeout_seconds=60),
            endpoints={"data": EndpointConfig()},
        ),
    }

    result = await list_apis(uuid4(), recorder, api_configs)
    api_entry = result["data"][0]

    assert "auth" not in api_entry
    assert "logging" not in api_entry
    assert "limits" not in api_entry

    assert api_entry["name"] == "secure_api"
    assert api_entry["type"] == "rest"
    assert api_entry["base_url"] == "https://secure.example.com"
    assert api_entry["endpoints"] == ["data"]


async def test_list_apis_records_one_audit_one_usage_one_insight(
    recorder: Recorder, tmp_path: Path
) -> None:
    """Each list_apis invocation MUST write exactly one event per category."""
    api_configs = {
        "test_api": ApiConfig(
            type="rest",
            base_url="https://test.example.com",
            endpoints={},
        ),
    }

    result = await list_apis(uuid4(), recorder, api_configs)
    assert "error" not in result

    # Stop the recorder to drain the queue and flush all writers.
    await recorder.stop()

    log_dir = tmp_path / "logs"
    audit_events = _read_jsonl_dir(log_dir / "audit")
    usage_events = _read_jsonl_dir(log_dir / "usage")
    insight_events = _read_jsonl_dir(log_dir / "insight")

    assert len(audit_events) == 1, audit_events
    assert audit_events[0]["tool"] == "list_apis"
    assert audit_events[0]["result"] == "success"

    assert len(usage_events) == 1, usage_events
    assert usage_events[0]["tool"] == "list_apis"
    assert usage_events[0]["status"] == "success"

    assert len(insight_events) == 1, insight_events
    assert insight_events[0]["tool"] == "list_apis"


async def test_list_apis_empty_config(recorder: Recorder) -> None:
    """list_apis with no configured APIs returns an empty data list."""
    result = await list_apis(uuid4(), recorder, {})

    assert result["data"] == []
    assert "metadata" in result


async def test_list_apis_endpoint_names(recorder: Recorder) -> None:
    """Endpoint names appear as a list of keys, not the full endpoint config."""
    api_configs = {
        "api_with_endpoints": ApiConfig(
            type="rest",
            base_url="https://example.com",
            endpoints={
                "get_data": EndpointConfig(),
                "post_data": EndpointConfig(),
                "delete_data": EndpointConfig(),
            },
        ),
    }

    result = await list_apis(uuid4(), recorder, api_configs)
    endpoints = result["data"][0]["endpoints"]

    assert isinstance(endpoints, list)
    assert sorted(endpoints) == ["delete_data", "get_data", "post_data"]


def _read_jsonl_dir(path: Path) -> list[dict]:
    """Read all JSONL files under `path` and return parsed events."""
    if not path.exists():
        return []
    events: list[dict] = []
    for jsonl in sorted(path.glob("*.jsonl")):
        with jsonl.open() as f:
            events.extend(json.loads(line) for line in f if line.strip())
    return events
