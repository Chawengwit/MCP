"""Subprocess smoke test: `python -m src.server` boots, accepts SIGTERM, exits cleanly.

This test confirms the wiring chain end-to-end at the OS-process level:
  - Module import path works (`python -m src.server`)
  - Recorder.start() doesn't deadlock during boot
  - Signal handlers are installed and SIGTERM drains the Recorder queue
  - Stdout stays empty (MCP protocol channel — must not be polluted by app logs)

Phase 3-5 unit tests cover the in-process behavior; this test catches regressions
that only show up when the server runs as a real OS process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

READY_MARKER = "MCP server started"
SHUTDOWN_GRACE_SEC = 10.0
BOOT_DEADLINE_SEC = 8.0


def _spawn_server(env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Spawn the server in a subprocess with stdin closed.

    Stdin is closed so the MCP stdio_server immediately sees EOF and the only
    way out is a signal — keeps the test deterministic.

    PYTHONUNBUFFERED=1 prevents block-buffering of stderr through the pipe
    (defense-in-depth: the server uses sys.stderr.print which is line-buffered,
    but a future contributor switching to raw `print()` would otherwise hang
    `_wait_for_ready` waiting for a buffer flush).
    """
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [sys.executable, "-m", "src.server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """Return a base env dict that isolates the subprocess from any local config.

    Pointing MCP_API_CONFIG_PATH at a nonexistent file makes `load_api_configs`
    return an empty dict regardless of what's in the developer's working tree.
    Without this, a populated `config/api_configs.json` (with unresolved ${VAR}
    placeholders) would crash the server before it reached the ready marker.
    """
    env = os.environ.copy()
    env["MCP_LOG_DIR"] = str(tmp_path / "logs")
    env["MCP_LOG_BUFFER_SIZE"] = "1"
    env["MCP_API_CONFIG_PATH"] = str(tmp_path / "no_such_config.json")
    return env


def _wait_for_ready(proc: subprocess.Popen[bytes], deadline_sec: float) -> bytes:
    """Read stderr until the ready marker is seen or the deadline passes.

    Returns the captured stderr fragment so tests can assert log content. Raises
    TimeoutError if the marker doesn't appear in time — that's a real failure
    of the boot path, not test flakiness.
    """
    assert proc.stderr is not None
    captured = bytearray()
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        # Non-blocking-ish read with short timeout via select isn't portable in
        # subprocess; instead, read line-by-line. Lines are flushed eagerly by
        # the server's _log() (each call ends with '\n').
        line = proc.stderr.readline()
        if not line:
            # Process likely exited; let the caller handle returncode.
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Server exited before ready marker; returncode={proc.returncode}, "
                    f"stderr so far:\n{captured.decode(errors='replace')}"
                )
            time.sleep(0.05)
            continue
        captured.extend(line)
        if READY_MARKER.encode() in line:
            return bytes(captured)
    raise TimeoutError(
        f"Ready marker {READY_MARKER!r} not seen within {deadline_sec}s. "
        f"Stderr so far:\n{captured.decode(errors='replace')}"
    )


def test_server_boots_and_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    """Spawn the server, wait for boot, send SIGTERM, assert clean exit + empty stdout."""
    env = _isolated_env(tmp_path)
    proc = _spawn_server(env)
    try:
        boot_log = _wait_for_ready(proc, BOOT_DEADLINE_SEC)
        assert b"Activity logging started" in boot_log
        assert b"Registered 5 tools" in boot_log

        proc.send_signal(signal.SIGTERM)
        try:
            stdout, stderr_rest = proc.communicate(timeout=SHUTDOWN_GRACE_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr_rest = proc.communicate()
            pytest.fail(
                f"Server did not exit within {SHUTDOWN_GRACE_SEC}s of SIGTERM. "
                f"Forced kill. stderr:\n{(boot_log + stderr_rest).decode(errors='replace')}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    full_stderr = boot_log + stderr_rest

    # Clean exit: 0 (graceful), -SIGTERM (signal received but stop drained), or
    # the platform-specific positive equivalent. Negative codes are POSIX
    # convention for "killed by signal N".
    assert proc.returncode in (0, -signal.SIGTERM, signal.SIGTERM), (
        f"Unexpected exit code {proc.returncode}\nstderr:\n{full_stderr.decode(errors='replace')}"
    )

    # Stdout MUST be empty — MCP protocol channel, even one stray byte breaks clients.
    assert stdout == b"", (
        f"Server wrote to stdout (would corrupt MCP protocol stream):\n"
        f"{stdout.decode(errors='replace')}"
    )

    # Recorder drained — the shutdown log lines must be present.
    assert b"MCP server stopped" in full_stderr, (
        f"Recorder shutdown line missing — drain may have been skipped.\n"
        f"stderr:\n{full_stderr.decode(errors='replace')}"
    )


def test_server_does_not_write_secrets_to_stderr(tmp_path: Path) -> None:
    """Boot the server with a fake-secret env var and confirm it doesn't appear in stderr.

    Catches regressions where startup code accidentally echoes config values
    (e.g. logging the loaded api_configs as a dict).
    """
    env = _isolated_env(tmp_path)
    secret = "SHOULD_NOT_APPEAR_IN_STDERR_42"
    env["EXAMPLE_REST_CLIENT_SECRET"] = secret

    proc = _spawn_server(env)
    try:
        boot_log = _wait_for_ready(proc, BOOT_DEADLINE_SEC)
        proc.send_signal(signal.SIGTERM)
        _, stderr_rest = proc.communicate(timeout=SHUTDOWN_GRACE_SEC)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    combined = (boot_log + stderr_rest).decode("utf-8", errors="replace")
    assert secret not in combined, "Server logged a secret env var value to stderr"


def test_server_jsonl_logs_have_no_secrets(tmp_path: Path) -> None:
    """After a boot/shutdown cycle, scan every JSONL event for known-secret tokens."""
    env = _isolated_env(tmp_path)
    log_dir = tmp_path / "logs"
    canary_token = "JSONL_LEAK_CANARY_98765"
    env["EXAMPLE_REST_CLIENT_SECRET"] = canary_token

    proc = _spawn_server(env)
    try:
        _wait_for_ready(proc, BOOT_DEADLINE_SEC)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=SHUTDOWN_GRACE_SEC)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    if not log_dir.exists():
        # No tools were invoked, no JSONL written — that's also a pass: nothing to leak.
        return

    for category_dir in log_dir.iterdir():
        if not category_dir.is_dir():
            continue
        for jsonl_file in category_dir.iterdir():
            for line in jsonl_file.read_text().splitlines():
                if not line.strip():
                    continue
                # Each line must be valid JSON and must not contain the canary.
                json.loads(line)
                assert canary_token not in line, f"Canary {canary_token!r} leaked into {jsonl_file}"
