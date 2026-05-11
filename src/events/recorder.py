from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from .schemas import (
    AuditEvent,
    Category,
    DebugEvent,
    HttpRequestRecord,
    HttpResponseRecord,
    InsightEvent,
    ResponseSummary,
    UsageEvent,
)
from .writers import JsonlWriter, WriterConfig


class Recorder:
    """High-level event recording API.

    A single instance is held by the MCP server. Tools/gateway/auth modules
    call `record_*` methods which never raise on internal failure (errors
    are emitted to stderr only).
    """

    def __init__(self, writer: JsonlWriter) -> None:
        self._writer = writer

    @classmethod
    def from_env(cls) -> Recorder:
        log_dir = Path(os.getenv("MCP_LOG_DIR", "./logs"))
        retention_days = int(os.getenv("MCP_LOG_RETENTION_DAYS", "365"))
        flush_interval = float(os.getenv("MCP_LOG_FLUSH_INTERVAL_SEC", "5"))
        buffer_size = int(os.getenv("MCP_LOG_BUFFER_SIZE", "100"))

        enabled: set[Category] = set()
        for category, env_var in [
            (Category.AUDIT, "MCP_LOG_AUDIT_ENABLED"),
            (Category.DEBUG, "MCP_LOG_DEBUG_ENABLED"),
            (Category.USAGE, "MCP_LOG_USAGE_ENABLED"),
            (Category.INSIGHT, "MCP_LOG_INSIGHT_ENABLED"),
        ]:
            if _truthy(os.getenv(env_var, "true")):
                enabled.add(category)

        config = WriterConfig(
            log_dir=log_dir,
            retention_days=retention_days,
            flush_interval_sec=flush_interval,
            buffer_size=buffer_size,
            enabled_categories=frozenset(enabled),
        )
        return cls(JsonlWriter(config))

    async def start(self) -> None:
        await self._writer.start()

    async def stop(self) -> None:
        await self._writer.stop()

    async def record_audit(
        self,
        *,
        session_id: UUID,
        tool: str,
        result: str,
        duration_ms: int,
        api: str | None = None,
        endpoint: str | None = None,
        requires_auth: bool = False,
        auth_method: str | None = None,
        note: str | None = None,
        user_id: str | None = None,
    ) -> None:
        try:
            event = AuditEvent(
                session_id=session_id,
                user_id=user_id,
                tool=tool,
                api=api,
                endpoint=endpoint,
                result=result,  # type: ignore[arg-type]
                duration_ms=duration_ms,
                requires_auth=requires_auth,
                auth_method=auth_method,
                note=note,
            )
            await self._writer.submit(event)
        except Exception as exc:
            _warn(f"record_audit failed: {exc}")

    async def record_debug(
        self,
        *,
        session_id: UUID,
        tool: str,
        duration_ms: int,
        api: str | None = None,
        request: HttpRequestRecord | None = None,
        response: HttpResponseRecord | None = None,
        error: str | None = None,
    ) -> None:
        try:
            event = DebugEvent(
                session_id=session_id,
                tool=tool,
                api=api,
                request=request,
                response=response,
                duration_ms=duration_ms,
                error=error,
            )
            await self._writer.submit(event)
        except Exception as exc:
            _warn(f"record_debug failed: {exc}")

    async def record_usage(
        self,
        *,
        tool: str,
        status: str,
        duration_ms: int,
        api: str | None = None,
        endpoint: str | None = None,
        request_bytes: int = 0,
        response_bytes: int = 0,
        user_id: str | None = None,
    ) -> None:
        try:
            event = UsageEvent(
                tool=tool,
                user_id=user_id,
                api=api,
                endpoint=endpoint,
                status=status,  # type: ignore[arg-type]
                duration_ms=duration_ms,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )
            await self._writer.submit(event)
        except Exception as exc:
            _warn(f"record_usage failed: {exc}")

    async def record_insight(
        self,
        *,
        session_id: UUID,
        tool: str,
        tool_args: dict[str, Any],
        api: str | None = None,
        response_summary: ResponseSummary | None = None,
        user_id: str | None = None,
    ) -> None:
        try:
            event = InsightEvent(
                session_id=session_id,
                user_id=user_id,
                tool=tool,
                api=api,
                tool_args=tool_args,
                response_summary=response_summary,
            )
            await self._writer.submit(event)
        except Exception as exc:
            _warn(f"record_insight failed: {exc}")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _warn(msg: str) -> None:
    print(f"[mcp.events.recorder] {msg}", file=sys.stderr)
