from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .spec import ToolSpec


class ToolRegistry:
    """In-memory registry of available MCP tools.

    Construct one per server lifetime; do not share across server instances.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a tool.

        Raises:
            ValueError: If tool name already registered
        """
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """Retrieve a registered tool by name; returns None if not found."""
        return self._tools.get(name)

    def all(self) -> dict[str, ToolSpec]:
        """Return all registered tools as a new dict (caller cannot mutate)."""
        return dict(self._tools)

    def __len__(self) -> int:
        return len(self._tools)
