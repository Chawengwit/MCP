"""Activity logging for MCP Data Gateway.

Records four categories of events to JSONL files split by month:
- audit: who/when/what (security focus)
- debug: full HTTP exchange (with redaction)
- usage: per-call metrics
- insight: what Claude asked for

Logs are operator-only and not exposed via MCP tools.
"""

from .recorder import Recorder
from .redaction import redact_body, redact_headers, redact_url
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

__all__ = [
    "Recorder",
    "JsonlWriter",
    "WriterConfig",
    "Category",
    "AuditEvent",
    "DebugEvent",
    "UsageEvent",
    "InsightEvent",
    "HttpRequestRecord",
    "HttpResponseRecord",
    "ResponseSummary",
    "redact_body",
    "redact_headers",
    "redact_url",
]
