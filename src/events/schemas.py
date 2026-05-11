from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Category(str, Enum):
    AUDIT = "audit"
    DEBUG = "debug"
    USAGE = "usage"
    INSIGHT = "insight"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class _BaseEvent(BaseModel):
    timestamp: datetime = Field(default_factory=_now_utc)
    event_id: UUID = Field(default_factory=uuid4)

    model_config = {"extra": "forbid"}


class AuditEvent(_BaseEvent):
    category: Literal[Category.AUDIT] = Category.AUDIT
    session_id: UUID
    user_id: str | None = None  # Phase 9: per-user identity when OAuth Provider is on
    tool: str
    api: str | None = None
    endpoint: str | None = None
    result: Literal["success", "denied", "error"]
    duration_ms: int
    requires_auth: bool = False
    auth_method: str | None = None
    note: str | None = None


class HttpRequestRecord(BaseModel):
    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None

    model_config = {"extra": "forbid"}


class HttpResponseRecord(BaseModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None

    model_config = {"extra": "forbid"}


class DebugEvent(_BaseEvent):
    category: Literal[Category.DEBUG] = Category.DEBUG
    session_id: UUID
    tool: str
    api: str | None = None
    request: HttpRequestRecord | None = None
    response: HttpResponseRecord | None = None
    duration_ms: int
    error: str | None = None


class UsageEvent(_BaseEvent):
    category: Literal[Category.USAGE] = Category.USAGE
    tool: str
    user_id: str | None = None  # Phase 9
    api: str | None = None
    endpoint: str | None = None
    status: Literal["success", "error"]
    duration_ms: int
    request_bytes: int = 0
    response_bytes: int = 0


class ResponseSummary(BaseModel):
    type: str
    item_count: int | None = None
    size_bytes: int = 0
    top_keys: list[str] | None = None

    model_config = {"extra": "forbid"}


class InsightEvent(_BaseEvent):
    category: Literal[Category.INSIGHT] = Category.INSIGHT
    session_id: UUID
    user_id: str | None = None  # Phase 9
    tool: str
    api: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    response_summary: ResponseSummary | None = None


Event = AuditEvent | DebugEvent | UsageEvent | InsightEvent
