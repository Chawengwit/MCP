from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from src.events.recorder import Recorder, _truthy
from src.events.schemas import Category, ResponseSummary
from src.events.writers import JsonlWriter, WriterConfig

# ---------------------------------------------------------------------------
# _truthy helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("  yes  ", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("garbage", False),
        ("", False),
    ],
)
def test_truthy_parsing(value: str, expected: bool) -> None:
    assert _truthy(value) is expected


# ---------------------------------------------------------------------------
# Recorder.from_env — environment variable parsing
# ---------------------------------------------------------------------------


def test_from_env_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [
        "MCP_LOG_DIR",
        "MCP_LOG_RETENTION_DAYS",
        "MCP_LOG_FLUSH_INTERVAL_SEC",
        "MCP_LOG_BUFFER_SIZE",
        "MCP_LOG_AUDIT_ENABLED",
        "MCP_LOG_DEBUG_ENABLED",
        "MCP_LOG_USAGE_ENABLED",
        "MCP_LOG_INSIGHT_ENABLED",
    ]:
        monkeypatch.delenv(var, raising=False)

    recorder = Recorder.from_env()
    config = recorder._writer._config
    assert config.log_dir == Path("./logs")
    assert config.retention_days == 365
    assert config.flush_interval_sec == 5.0
    assert config.buffer_size == 100
    assert config.enabled_categories == frozenset(Category)


def test_from_env_disables_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_LOG_AUDIT_ENABLED", "true")
    monkeypatch.setenv("MCP_LOG_DEBUG_ENABLED", "false")
    monkeypatch.setenv("MCP_LOG_USAGE_ENABLED", "off")
    monkeypatch.setenv("MCP_LOG_INSIGHT_ENABLED", "no")

    recorder = Recorder.from_env()
    config = recorder._writer._config
    assert config.enabled_categories == frozenset({Category.AUDIT})


def test_from_env_typo_disables_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented behavior: anything not in the truthy set disables the category."""
    monkeypatch.setenv("MCP_LOG_AUDIT_ENABLED", "ture")  # typo
    recorder = Recorder.from_env()
    config = recorder._writer._config
    assert Category.AUDIT not in config.enabled_categories


def test_from_env_invalid_int_raises_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad numeric env vars fail fast at startup, not at runtime."""
    monkeypatch.setenv("MCP_LOG_RETENTION_DAYS", "not-a-number")
    with pytest.raises(ValueError):
        Recorder.from_env()


def test_from_env_uses_custom_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path / "custom_logs"))
    monkeypatch.setenv("MCP_LOG_RETENTION_DAYS", "30")
    recorder = Recorder.from_env()
    config = recorder._writer._config
    assert config.log_dir == tmp_path / "custom_logs"
    assert config.retention_days == 30


# ---------------------------------------------------------------------------
# never-raises invariant
# ---------------------------------------------------------------------------


def _make_recorder(tmp_path: Path) -> Recorder:
    writer = JsonlWriter(
        WriterConfig(
            log_dir=tmp_path,
            flush_interval_sec=0.05,
            buffer_size=10,
        )
    )
    return Recorder(writer)


async def test_record_audit_never_raises_when_writer_not_started(
    tmp_path: Path,
) -> None:
    recorder = _make_recorder(tmp_path)
    # writer.start() never called → submit() raises RuntimeError, recorder swallows
    await recorder.record_audit(
        session_id=uuid4(), tool="fetch_data", result="success", duration_ms=10
    )
    # If we got here without raising, the invariant holds.


async def test_record_usage_never_raises_on_invalid_status(tmp_path: Path) -> None:
    recorder = _make_recorder(tmp_path)
    await recorder.start()
    try:
        # status="bogus" fails Pydantic Literal validation; recorder must swallow
        await recorder.record_usage(tool="fetch_data", status="bogus", duration_ms=10)
    finally:
        await recorder.stop()


async def test_record_audit_writes_event_end_to_end(tmp_path: Path) -> None:
    recorder = _make_recorder(tmp_path)
    await recorder.start()
    try:
        session_id = uuid4()
        await recorder.record_audit(
            session_id=session_id,
            tool="fetch_data",
            api="example_api",
            endpoint="get_users",
            result="success",
            duration_ms=42,
            requires_auth=True,
            auth_method="oauth2",
        )
    finally:
        await recorder.stop()

    audit_files = list((tmp_path / "audit").glob("*.jsonl"))
    assert len(audit_files) == 1
    payload = json.loads(audit_files[0].read_text().strip())
    assert payload["category"] == "audit"
    assert payload["tool"] == "fetch_data"
    assert payload["api"] == "example_api"
    assert payload["session_id"] == str(session_id)
    assert payload["result"] == "success"
    assert payload["requires_auth"] is True


async def test_record_all_four_categories_end_to_end(tmp_path: Path) -> None:
    recorder = _make_recorder(tmp_path)
    await recorder.start()
    try:
        session_id = uuid4()
        await recorder.record_audit(
            session_id=session_id, tool="fetch_data", result="success", duration_ms=1
        )
        await recorder.record_usage(tool="fetch_data", status="success", duration_ms=1)
        await recorder.record_insight(
            session_id=session_id,
            tool="fetch_data",
            tool_args={"x": 1},
            response_summary=ResponseSummary(type="list", item_count=1, size_bytes=10),
        )
        await recorder.record_debug(
            session_id=session_id,
            tool="fetch_data",
            duration_ms=1,
        )
    finally:
        await recorder.stop()

    for category in ["audit", "usage", "insight", "debug"]:
        files = list((tmp_path / category).glob("*.jsonl"))
        assert len(files) == 1, f"missing file for {category}"
        line = files[0].read_text().strip()
        assert line, f"empty file for {category}"
        payload = json.loads(line)
        assert payload["category"] == category
