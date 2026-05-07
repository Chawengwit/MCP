from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

ToolHandler = Callable[[UUID, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    """Schema for a tool that Claude can invoke.

    Fields:
        name: Identifier for the tool (e.g., "list_apis")
        description: Human-readable description shown to Claude
        input_schema: JSONSchema dict describing tool input
        handler: Async callable invoked as `handler(session_id, arguments)`.
                 Returns the tool response dict per CLAUDE.md (data+metadata or error).
                 Dependencies (Recorder, configs, etc.) are bound via closure at
                 registration time.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
