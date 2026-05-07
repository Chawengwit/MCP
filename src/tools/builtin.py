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
                "endpoints": list(api_config.endpoints.keys()),
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
