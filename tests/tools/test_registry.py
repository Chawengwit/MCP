from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from src.tools import ToolRegistry, ToolSpec


async def _noop_handler(session_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"data": [], "metadata": {}}


def _make_spec(name: str, description: str = "test") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        handler=_noop_handler,
    )


def test_registry_register_and_get() -> None:
    registry = ToolRegistry()
    spec = _make_spec("test_tool", "A test tool")

    registry.register(spec)
    retrieved = registry.get("test_tool")

    assert retrieved is not None
    assert retrieved.name == "test_tool"
    assert retrieved.description == "A test tool"


def test_registry_get_nonexistent() -> None:
    registry = ToolRegistry()
    assert registry.get("nonexistent_tool") is None


def test_registry_duplicate_name_raises() -> None:
    registry = ToolRegistry()
    registry.register(_make_spec("duplicate_tool"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_make_spec("duplicate_tool"))


def test_registry_all_returns_copy() -> None:
    registry = ToolRegistry()
    registry.register(_make_spec("tool1"))
    registry.register(_make_spec("tool2"))

    snapshot = registry.all()
    assert set(snapshot.keys()) == {"tool1", "tool2"}

    # Mutating the returned dict must not affect the registry.
    snapshot.clear()
    assert len(registry) == 2


def test_registry_len() -> None:
    registry = ToolRegistry()
    assert len(registry) == 0

    registry.register(_make_spec("test_tool"))
    assert len(registry) == 1
