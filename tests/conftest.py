from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from src.events import Category, JsonlWriter, Recorder, WriterConfig

if TYPE_CHECKING:
    pass


@pytest.fixture
async def recorder(tmp_path: Path) -> AsyncIterator[Recorder]:
    """Recorder backed by a temp dir with all categories enabled."""
    config = WriterConfig(
        log_dir=tmp_path / "logs",
        retention_days=1,
        enabled_categories=frozenset(Category),
    )
    rec = Recorder(JsonlWriter(config))
    await rec.start()
    try:
        yield rec
    finally:
        await rec.stop()
