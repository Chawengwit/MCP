from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from src.events.schemas import (
    AuditEvent,
    Category,
    DebugEvent,
    HttpRequestRecord,
    HttpResponseRecord,
    InsightEvent,
    ResponseSummary,
    UsageEvent,
)


def test_audit_event_minimum_fields() -> None:
    event = AuditEvent(
        session_id=uuid4(),
        tool="fetch_data",
        result="success",
        duration_ms=42,
    )
    assert event.category == Category.AUDIT
    assert event.event_id is not None
    assert event.timestamp.tzinfo is not None


def test_audit_event_invalid_result() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(
            session_id=uuid4(),
            tool="fetch_data",
            result="bogus",  # type: ignore[arg-type]
            duration_ms=0,
        )


def test_audit_event_serializes_to_json() -> None:
    event = AuditEvent(
        session_id=uuid4(),
        tool="fetch_data",
        api="example_api",
        result="success",
        duration_ms=100,
    )
    payload = json.loads(event.model_dump_json())
    assert payload["category"] == "audit"
    assert payload["tool"] == "fetch_data"
    assert payload["api"] == "example_api"
    assert payload["result"] == "success"


def test_debug_event_with_request_response() -> None:
    event = DebugEvent(
        session_id=uuid4(),
        tool="fetch_data",
        api="example_api",
        request=HttpRequestRecord(
            method="GET",
            url="https://api.example.com/users",
            headers={"Authorization": "Bearer xxx"},
        ),
        response=HttpResponseRecord(status=200, headers={}, body={"users": []}),
        duration_ms=120,
    )
    assert event.category == Category.DEBUG
    assert event.request is not None
    assert event.request.method == "GET"
    assert event.response is not None
    assert event.response.status == 200


def test_usage_event_defaults() -> None:
    event = UsageEvent(
        tool="fetch_data",
        status="success",
        duration_ms=10,
    )
    assert event.category == Category.USAGE
    assert event.request_bytes == 0
    assert event.response_bytes == 0


def test_insight_event_with_summary() -> None:
    event = InsightEvent(
        session_id=uuid4(),
        tool="fetch_data",
        tool_args={"filters": {"active": True}},
        response_summary=ResponseSummary(
            type="list", item_count=50, size_bytes=4096, top_keys=["users"]
        ),
    )
    assert event.category == Category.INSIGHT
    assert event.tool_args == {"filters": {"active": True}}
    assert event.response_summary is not None
    assert event.response_summary.item_count == 50


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        UsageEvent(
            tool="fetch_data",
            status="success",
            duration_ms=10,
            unknown_field="x",  # type: ignore[call-arg]
        )
