from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.config import ApiConfig
from src.events import Recorder


async def list_apis(
    session_id: UUID,
    recorder: Recorder,
    api_configs: dict[str, ApiConfig],
) -> dict[str, Any]:
    """List all configured APIs.

    Returns only: name, type, base_url, endpoints (list of names).
    Omits: auth, logging, limits.

    Args:
        session_id: Session identifier for audit logging
        recorder: Recorder for activity logging
        api_configs: Loaded API configurations

    Returns:
        dict with data and metadata fields per CLAUDE.md Response Format Conventions
    """
    start_perf = time.perf_counter()

    try:
        data = [
            {
                "name": api_name,
                "type": api_config.type,
                "base_url": api_config.base_url,
                "endpoints": _serialize_endpoints(api_config),
            }
            for api_name, api_config in api_configs.items()
        ]

        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        response_bytes = len(json.dumps(data).encode("utf-8"))

        await recorder.record_audit(
            session_id=session_id,
            tool="list_apis",
            result="success",
            duration_ms=duration_ms,
        )
        await recorder.record_usage(
            tool="list_apis",
            status="success",
            duration_ms=duration_ms,
            response_bytes=response_bytes,
        )
        await recorder.record_insight(
            session_id=session_id,
            tool="list_apis",
            tool_args={},
        )

        return {
            "data": data,
            "metadata": {
                "source": "mcp_data_gateway",
                "endpoint": "list_apis",
                "timestamp": _now_iso(),
                "duration_ms": duration_ms,
            },
        }
    except Exception as e:
        duration_ms = int((time.perf_counter() - start_perf) * 1000)

        await recorder.record_audit(
            session_id=session_id,
            tool="list_apis",
            result="error",
            duration_ms=duration_ms,
        )
        await recorder.record_usage(
            tool="list_apis",
            status="error",
            duration_ms=duration_ms,
        )

        return {
            "error": {
                "code": "TOOL_ERROR",
                "message": f"list_apis failed: {str(e)}",
                "details": {"exception_type": type(e).__name__},
            }
        }


def _now_iso() -> str:
    """Return current time in ISO 8601 format (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _serialize_endpoints(api_config: ApiConfig) -> list[dict[str, Any]]:
    """Return the LLM-facing endpoint metadata for ``list_apis``.

    Each entry includes the basic shape (``name``, ``method``, ``path``,
    ``query_params``) plus the Phase 9.5 hints (``description``,
    ``required_params``, ``param_hints``) when set. Hints are omitted
    when ``None`` so the response stays compact for endpoints that
    haven't been documented yet — LLMs treat absent fields as
    "no information" rather than "no constraint".

    Returning a richer ``list[dict]`` is a deliberate replacement for
    the older ``list[str]`` of just endpoint names. The LLM sees every
    endpoint's constraints before it ever attempts a call, so prompts
    like *"get me 3 subscribers"* resolve to a correct ``fetch_data``
    call on the first try instead of probing the upstream API.
    """
    out: list[dict[str, Any]] = []
    for endpoint_name, endpoint in api_config.endpoints.items():
        entry: dict[str, Any] = {"name": endpoint_name}
        if endpoint.method:
            entry["method"] = endpoint.method.upper()
        if endpoint.path:
            entry["path"] = endpoint.path
        if endpoint.query_params:
            entry["query_params"] = list(endpoint.query_params)
        if endpoint.description:
            entry["description"] = endpoint.description
        if endpoint.required_params:
            entry["required_params"] = list(endpoint.required_params)
        if endpoint.param_hints:
            entry["param_hints"] = dict(endpoint.param_hints)
        out.append(entry)
    return out
