from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import IO

from .retention import cleanup_with_warnings
from .schemas import Category, Event


@dataclass
class WriterConfig:
    log_dir: Path
    retention_days: int = 365
    flush_interval_sec: float = 5.0
    buffer_size: int = 100
    queue_max_size: int = 10_000
    enabled_categories: frozenset[Category] = field(
        default_factory=lambda: frozenset(Category)
    )


@dataclass
class _OpenFile:
    handle: IO[str]
    month: date  # first-of-month date this handle represents


class JsonlWriter:
    """Async writer that consumes events from a queue and appends JSONL.

    One file per category per month: <log_dir>/<category>/YYYY-MM.jsonl.
    On month rollover (per category), close the old handle, open a new one,
    and trigger a non-blocking retention cleanup.
    """

    def __init__(self, config: WriterConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(
            maxsize=config.queue_max_size
        )
        self._task: asyncio.Task[None] | None = None
        self._handles: dict[Category, _OpenFile] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._dropped_count = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._config.log_dir.mkdir(parents=True, exist_ok=True)
        for category in self._config.enabled_categories:
            (self._config.log_dir / category.value).mkdir(
                parents=True, exist_ok=True
            )
        self._task = asyncio.create_task(self._run(), name="mcp.events.writer")
        # Sweep stale files left from prior runs (handles restart-with-old-logs).
        self._schedule_cleanup()

    async def submit(self, event: Event) -> None:
        if self._task is None:
            raise RuntimeError("JsonlWriter not started")
        if event.category not in self._config.enabled_categories:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1
            print(
                f"[mcp.events.writer] queue full, dropped event "
                f"(category={event.category.value}, total_dropped={self._dropped_count})",
                file=sys.stderr,
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)  # sentinel
        await self._task
        self._task = None
        self._close_all_handles()
        if self._cleanup_task is not None:
            try:
                await self._cleanup_task
            except Exception:
                pass

    async def _run(self) -> None:
        buffer: list[Event] = []
        loop = asyncio.get_running_loop()
        last_flush = loop.time()
        while True:
            timeout = max(
                0.0,
                self._config.flush_interval_sec - (loop.time() - last_flush),
            )
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                self._flush(buffer)
                buffer.clear()
                last_flush = loop.time()
                continue

            if event is None:
                # sentinel: drain remaining queue then exit
                while not self._queue.empty():
                    try:
                        next_event = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if next_event is None:
                        continue
                    buffer.append(next_event)
                self._flush(buffer)
                buffer.clear()
                return

            buffer.append(event)
            if len(buffer) >= self._config.buffer_size:
                self._flush(buffer)
                buffer.clear()
                last_flush = loop.time()

    def _flush(self, events: list[Event]) -> None:
        if not events:
            return
        rolled_categories: set[Category] = set()
        for event in events:
            try:
                handle, rolled = self._get_handle_for(event)
                if rolled:
                    rolled_categories.add(event.category)
                handle.write(self._serialize(event) + "\n")
            except Exception as exc:
                print(
                    f"[mcp.events.writer] failed to write event: {exc}",
                    file=sys.stderr,
                )
        for category in self._handles:
            try:
                self._handles[category].handle.flush()
            except Exception as exc:
                print(
                    f"[mcp.events.writer] flush failed for {category.value}: {exc}",
                    file=sys.stderr,
                )
        if rolled_categories:
            self._schedule_cleanup()

    def _get_handle_for(self, event: Event) -> tuple[IO[str], bool]:
        ts: datetime = event.timestamp
        month_first = date(ts.year, ts.month, 1)
        existing = self._handles.get(event.category)
        if existing is not None and existing.month == month_first:
            return existing.handle, False
        is_true_rollover = existing is not None
        if existing is not None:
            try:
                existing.handle.close()
            except Exception:
                pass
        path = self._path_for(event.category, month_first)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
        self._handles[event.category] = _OpenFile(handle=handle, month=month_first)
        # Trigger cleanup only on a true month rollover within this process.
        # First-time category creation skips cleanup (start() already swept).
        return handle, is_true_rollover

    def _path_for(self, category: Category, month_first: date) -> Path:
        return (
            self._config.log_dir
            / category.value
            / f"{month_first.year:04d}-{month_first.month:02d}.jsonl"
        )

    def _schedule_cleanup(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(
            self._run_cleanup(), name="mcp.events.cleanup"
        )

    async def _run_cleanup(self) -> None:
        try:
            await asyncio.to_thread(
                cleanup_with_warnings,
                self._config.log_dir,
                self._config.retention_days,
            )
        except Exception as exc:
            print(
                f"[mcp.events.writer] cleanup task failed: {exc}",
                file=sys.stderr,
            )

    def _close_all_handles(self) -> None:
        for open_file in self._handles.values():
            try:
                open_file.handle.flush()
                open_file.handle.close()
            except Exception:
                pass
        self._handles.clear()

    @staticmethod
    def _serialize(event: Event) -> str:
        return event.model_dump_json(exclude_none=False)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
