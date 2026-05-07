from __future__ import annotations

from datetime import date
from pathlib import Path

from src.events.retention import cleanup_old_logs


def _make(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_cleanup_deletes_files_older_than_retention(tmp_path: Path) -> None:
    today = date(2026, 5, 15)
    _make(tmp_path / "audit" / "2024-01.jsonl")  # ~16 months old → delete
    _make(tmp_path / "audit" / "2025-12.jsonl")  # 5 months old → keep
    _make(tmp_path / "audit" / "2026-05.jsonl")  # current month → keep

    result = cleanup_old_logs(tmp_path, retention_days=365, today=today)

    deleted_names = sorted(p.name for p in result.deleted)
    assert deleted_names == ["2024-01.jsonl"]
    assert (tmp_path / "audit" / "2025-12.jsonl").exists()
    assert (tmp_path / "audit" / "2026-05.jsonl").exists()


def test_cleanup_never_deletes_current_month(tmp_path: Path) -> None:
    today = date(2026, 5, 15)
    # Even if retention is 0 days, current month must survive.
    _make(tmp_path / "debug" / "2026-05.jsonl")
    result = cleanup_old_logs(tmp_path, retention_days=0, today=today)
    assert result.deleted == []
    assert (tmp_path / "debug" / "2026-05.jsonl").exists()


def test_cleanup_walks_all_categories(tmp_path: Path) -> None:
    today = date(2026, 5, 15)
    for cat in ["audit", "debug", "usage", "insight"]:
        _make(tmp_path / cat / "2024-01.jsonl")
        _make(tmp_path / cat / "2026-05.jsonl")
    result = cleanup_old_logs(tmp_path, retention_days=365, today=today)
    assert len(result.deleted) == 4
    assert all(p.name == "2024-01.jsonl" for p in result.deleted)


def test_cleanup_ignores_non_matching_filenames(tmp_path: Path) -> None:
    today = date(2026, 5, 15)
    _make(tmp_path / "audit" / "README.md")
    _make(tmp_path / "audit" / "rotated.log")
    _make(tmp_path / "audit" / "2024-13.jsonl")  # invalid month
    result = cleanup_old_logs(tmp_path, retention_days=365, today=today)
    assert result.deleted == []


def test_cleanup_handles_missing_directories(tmp_path: Path) -> None:
    # No category dirs exist; should not raise.
    result = cleanup_old_logs(tmp_path, retention_days=365)
    assert result.deleted == []
    assert result.errors == []


def test_cleanup_oldest_deleted_correct(tmp_path: Path) -> None:
    today = date(2026, 5, 15)
    _make(tmp_path / "audit" / "2023-06.jsonl")
    _make(tmp_path / "audit" / "2024-01.jsonl")
    result = cleanup_old_logs(tmp_path, retention_days=365, today=today)
    assert result.oldest_deleted == date(2023, 6, 1)
