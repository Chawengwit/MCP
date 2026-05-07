from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .schemas import Category

FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")


@dataclass
class CleanupResult:
    deleted: list[Path]
    oldest_deleted: date | None
    errors: list[tuple[Path, str]]


def cleanup_old_logs(
    log_dir: Path,
    retention_days: int,
    *,
    today: date | None = None,
) -> CleanupResult:
    """Walk per-category log directories and delete files older than retention.

    The current month's file is never deleted, regardless of age.
    Errors are collected, never raised — caller decides what to do.
    """
    today = today or datetime.now(timezone.utc).date()
    deleted: list[Path] = []
    errors: list[tuple[Path, str]] = []

    for category in Category:
        cat_dir = log_dir / category.value
        if not cat_dir.is_dir():
            continue
        for path in cat_dir.iterdir():
            if not path.is_file():
                continue
            file_date = _parse_filename_date(path.name)
            if file_date is None:
                continue
            if _is_current_month(file_date, today):
                continue
            age_days = (today - file_date).days
            if age_days <= retention_days:
                continue
            try:
                path.unlink()
                deleted.append(path)
            except OSError as exc:
                errors.append((path, str(exc)))

    # Filter Nones so mypy can narrow to date; `deleted` only contains
    # paths whose filenames already parsed successfully above.
    parsed_dates = (d for p in deleted if (d := _parse_filename_date(p.name)) is not None)
    oldest_deleted = min(parsed_dates, default=None)
    return CleanupResult(deleted=deleted, oldest_deleted=oldest_deleted, errors=errors)


def _parse_filename_date(name: str) -> date | None:
    match = FILENAME_RE.match(name)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12):
        return None
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def _is_current_month(file_date: date, today: date) -> bool:
    return file_date.year == today.year and file_date.month == today.month


def cleanup_with_warnings(
    log_dir: Path, retention_days: int, *, today: date | None = None
) -> CleanupResult:
    """Run cleanup and emit warnings to stderr for any errors."""
    result = cleanup_old_logs(log_dir, retention_days, today=today)
    for path, msg in result.errors:
        print(
            f"[mcp.events.retention] failed to delete {path}: {msg}",
            file=sys.stderr,
        )
    return result
