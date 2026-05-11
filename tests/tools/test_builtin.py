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
    # Phase 9.5 — endpoints are dicts now; name is the only guaranteed field.
    assert api_entry["endpoints"] == [{"name": "data"}]


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
    """Each endpoint is serialised as a dict that always has a ``name`` field
    (Phase 9.5 enrichment; pre-Phase-9.5 tests asserted plain strings)."""
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
    names = sorted(e["name"] for e in endpoints)
    assert names == ["delete_data", "get_data", "post_data"]


# ----------------------------------------------------------------------
# Phase 9.5 — LLM-facing endpoint metadata
# ----------------------------------------------------------------------


async def test_list_apis_surfaces_endpoint_method_path_query_params(
    recorder: Recorder,
) -> None:
    """``list_apis`` includes ``method`` / ``path`` / ``query_params`` so the
    LLM can construct an exact call without guessing."""
    api_configs = {
        "svc": ApiConfig(
            type="rest",
            base_url="https://svc.example.com",
            endpoints={
                "search": EndpointConfig(
                    method="GET",
                    path="/v1/search",
                    query_params=["q", "limit"],
                ),
            },
        ),
    }
    result = await list_apis(uuid4(), recorder, api_configs)
    endpoint = result["data"][0]["endpoints"][0]
    assert endpoint["name"] == "search"
    assert endpoint["method"] == "GET"
    assert endpoint["path"] == "/v1/search"
    assert endpoint["query_params"] == ["q", "limit"]


async def test_list_apis_surfaces_description_required_params_and_hints(
    recorder: Recorder,
) -> None:
    """The Phase 9.5 metadata fields appear when set, in their canonical
    form, so an LLM reading the result can fill required params on the
    first try."""
    api_configs = {
        "svc": ApiConfig(
            type="rest",
            base_url="https://svc.example.com",
            endpoints={
                "list_things": EndpointConfig(
                    method="GET",
                    path="/v1/things",
                    query_params=["mode", "limit"],
                    description="List things in the service.",
                    required_params=["mode"],
                    param_hints={
                        "mode": "Filter mode — 'all' or 'active'",
                        "limit": "Max results, default 20",
                    },
                ),
            },
        ),
    }
    result = await list_apis(uuid4(), recorder, api_configs)
    endpoint = result["data"][0]["endpoints"][0]
    assert endpoint["description"] == "List things in the service."
    assert endpoint["required_params"] == ["mode"]
    assert endpoint["param_hints"]["mode"].startswith("Filter mode")


async def test_list_apis_omits_metadata_fields_when_unset(
    recorder: Recorder,
) -> None:
    """Endpoints without Phase 9.5 metadata MUST NOT carry empty hint /
    required_params keys — LLMs may treat presence-with-empty as 'no
    constraint, but I should ask'. Cleaner to omit entirely."""
    api_configs = {
        "svc": ApiConfig(
            type="rest",
            base_url="https://svc.example.com",
            endpoints={"bare": EndpointConfig(method="GET", path="/bare")},
        ),
    }
    result = await list_apis(uuid4(), recorder, api_configs)
    endpoint = result["data"][0]["endpoints"][0]
    assert "description" not in endpoint
    assert "required_params" not in endpoint
    assert "param_hints" not in endpoint
    assert "query_params" not in endpoint  # also omitted when unset


def _read_jsonl_dir(path: Path) -> list[dict]:
    """Read all JSONL files under `path` and return parsed events."""
    if not path.exists():
        return []
    events: list[dict] = []
    for jsonl in sorted(path.glob("*.jsonl")):
        with jsonl.open() as f:
            events.extend(json.loads(line) for line in f if line.strip())
    return events
