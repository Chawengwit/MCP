from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.events.schemas import (
    AuditEvent,
    Category,
    UsageEvent,
)
from src.events.writers import JsonlWriter, WriterConfig


def _audit(tool: str = "fetch_data", **kwargs) -> AuditEvent:
    return AuditEvent(
        session_id=uuid4(),
        tool=tool,
        result="success",
        duration_ms=1,
        **kwargs,
    )


def _usage(tool: str = "fetch_data") -> UsageEvent:
    return UsageEvent(tool=tool, status="success", duration_ms=1)


async def test_writer_appends_jsonl_per_category(tmp_path: Path) -> None:
    writer = JsonlWriter(
        WriterConfig(
            log_dir=tmp_path,
            flush_interval_sec=0.05,
            buffer_size=10,
        )
    )
    await writer.start()
    try:
        await writer.submit(_audit())
        await writer.submit(_usage())
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    audit_files = list((tmp_path / "audit").glob("*.jsonl"))
    usage_files = list((tmp_path / "usage").glob("*.jsonl"))
    assert len(audit_files) == 1
    assert len(usage_files) == 1

    audit_lines = audit_files[0].read_text().strip().splitlines()
    assert len(audit_lines) == 1
    payload = json.loads(audit_lines[0])
    assert payload["category"] == "audit"
    assert payload["tool"] == "fetch_data"


async def test_writer_filename_uses_event_month(tmp_path: Path) -> None:
    writer = JsonlWriter(WriterConfig(log_dir=tmp_path, flush_interval_sec=0.05, buffer_size=1))
    await writer.start()
    try:
        e = _audit()
        e.timestamp = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        await writer.submit(e)
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    files = list((tmp_path / "audit").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "2026-07.jsonl"


async def test_writer_skips_disabled_categories(tmp_path: Path) -> None:
    writer = JsonlWriter(
        WriterConfig(
            log_dir=tmp_path,
            flush_interval_sec=0.05,
            buffer_size=1,
            enabled_categories=frozenset({Category.AUDIT}),
        )
    )
    await writer.start()
    try:
        await writer.submit(_audit())
        await writer.submit(_usage())  # disabled → ignored
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    assert (tmp_path / "audit").exists()
    assert not (tmp_path / "usage").exists()


async def test_writer_buffers_and_flushes_on_threshold(tmp_path: Path) -> None:
    writer = JsonlWriter(
        WriterConfig(
            log_dir=tmp_path,
            flush_interval_sec=10.0,  # large; force buffer threshold to fire
            buffer_size=3,
        )
    )
    await writer.start()
    try:
        for _ in range(5):
            await writer.submit(_audit())
        await asyncio.sleep(0.1)
    finally:
        await writer.stop()

    audit_file = next((tmp_path / "audit").glob("*.jsonl"))
    lines = audit_file.read_text().strip().splitlines()
    assert len(lines) == 5


async def test_writer_drains_on_stop(tmp_path: Path) -> None:
    writer = JsonlWriter(
        WriterConfig(
            log_dir=tmp_path,
            flush_interval_sec=10.0,
            buffer_size=1000,  # large buffer; only stop() will flush
        )
    )
    await writer.start()
    for _ in range(7):
        await writer.submit(_audit())
    await writer.stop()

    audit_file = next((tmp_path / "audit").glob("*.jsonl"))
    lines = audit_file.read_text().strip().splitlines()
    assert len(lines) == 7
