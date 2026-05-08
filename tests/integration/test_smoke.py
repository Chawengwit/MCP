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
import signal
import subprocess
from pathlib import Path

import pytest

from tests.integration._helpers import (
    BOOT_DEADLINE_SEC,
    SHUTDOWN_GRACE_SEC,
    isolated_env,
    spawn_server,
    wait_for_ready,
)

READY_MARKER = "MCP server started"


def test_server_boots_and_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    """Spawn the server, wait for boot, send SIGTERM, assert clean exit + empty stdout."""
    env = isolated_env(tmp_path)
    proc = spawn_server(env)
    try:
        boot_log = wait_for_ready(proc, BOOT_DEADLINE_SEC, ready_marker=READY_MARKER)
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
    env = isolated_env(tmp_path)
    secret = "SHOULD_NOT_APPEAR_IN_STDERR_42"
    env["EXAMPLE_REST_CLIENT_SECRET"] = secret

    proc = spawn_server(env)
    try:
        boot_log = wait_for_ready(proc, BOOT_DEADLINE_SEC, ready_marker=READY_MARKER)
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
    env = isolated_env(tmp_path)
    log_dir = tmp_path / "logs"
    canary_token = "JSONL_LEAK_CANARY_98765"
    env["EXAMPLE_REST_CLIENT_SECRET"] = canary_token

    proc = spawn_server(env)
    try:
        wait_for_ready(proc, BOOT_DEADLINE_SEC, ready_marker=READY_MARKER)
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
