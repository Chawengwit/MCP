"""Subprocess smoke test for `MCP_TRANSPORT=http`.

Companion to `test_smoke.py` — that test exercises the stdio path.
This one boots the server in HTTP mode against a real TCP port:

  - Module import path works with MCP_TRANSPORT=http set in the env
  - uvicorn binds, accepts connections, and shuts down on SIGTERM
  - Bearer middleware enforces 401 / 200 at the OS-process level
  - Stdout stays empty (uvicorn's default loggers must remain silenced)
  - Bearer token literal does NOT leak into JSONL activity logs

The in-process tests under `tests/transport/` use Starlette's TestClient,
which doesn't open real sockets and doesn't run uvicorn. This test catches
regressions that only show up when uvicorn actually drives the app.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.integration._helpers import (
    BOOT_DEADLINE_SEC,
    SHUTDOWN_GRACE_SEC,
    spawn_server,
    wait_for_ready,
)
from tests.integration._helpers import isolated_env as base_isolated_env

READY_MARKER = "HTTP transport listening"
BEARER_TOKEN = "smoke-test-bearer-2c4f9a1e"


def _free_port() -> int:
    """Bind a TCP socket to port 0 to let the kernel pick a free one, then close."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_env(tmp_path: Path, port: int) -> dict[str, str]:
    """Extend the base isolated env with HTTP-transport variables."""
    env = base_isolated_env(tmp_path)
    env["MCP_TRANSPORT"] = "http"
    env["MCP_HTTP_HOST"] = "127.0.0.1"
    env["MCP_HTTP_PORT"] = str(port)
    env["MCP_HTTP_BEARER_TOKEN"] = BEARER_TOKEN
    return env


def _wait_for_listener(port: int, deadline_sec: float = 4.0) -> None:
    """Poll until the server's port accepts connections.

    The "HTTP transport listening" stderr line is printed before
    `uvicorn.Server.serve()` finishes binding, so we add a TCP-level wait to
    avoid a connection-refused race in the curl-equivalent below.
    """
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Port {port} not accepting connections within {deadline_sec}s")


def test_http_server_boots_authenticates_and_exits_cleanly(tmp_path: Path) -> None:
    """Boot in HTTP mode, hit /mcp with and without the token, then SIGTERM.

    Asserts:
      - 401 without bearer
      - 200 with bearer (initialize round-trip)
      - 404 on unknown path
      - Clean SIGTERM exit
      - Empty stdout
    """
    port = _free_port()
    env = _http_env(tmp_path, port)
    proc = spawn_server(env)
    try:
        boot_log = wait_for_ready(proc, BOOT_DEADLINE_SEC, ready_marker=READY_MARKER)
        assert f"http://127.0.0.1:{port}/mcp".encode() in boot_log
        assert b"bearer=set" in boot_log

        _wait_for_listener(port)

        url = f"http://127.0.0.1:{port}/mcp"
        accept = {"Accept": "application/json, text/event-stream"}
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "http-smoke", "version": "0.0.1"},
            },
        }

        with httpx.Client(timeout=5.0) as client:
            r_no_auth = client.post(url, json=init_body, headers=accept)
            assert r_no_auth.status_code == 401, r_no_auth.text

            r_ok = client.post(
                url,
                json=init_body,
                headers={**accept, "Authorization": f"Bearer {BEARER_TOKEN}"},
            )
            assert r_ok.status_code == 200, r_ok.text

            r_404 = client.get(
                f"http://127.0.0.1:{port}/wrong",
                headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            )
            assert r_404.status_code == 404

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

    assert proc.returncode in (0, -signal.SIGTERM, signal.SIGTERM), (
        f"Unexpected exit code {proc.returncode}\nstderr:\n{full_stderr.decode(errors='replace')}"
    )

    # Stdout must remain empty even in HTTP mode — uvicorn's default loggers
    # are silenced via log_config=None / access_log=False.
    assert stdout == b"", (
        f"Server wrote to stdout in HTTP mode (uvicorn loggers should be silent):\n"
        f"{stdout.decode(errors='replace')}"
    )


def test_http_loopback_guard_refuses_public_bind_without_token(tmp_path: Path) -> None:
    """Refuse to start when MCP_HTTP_HOST is non-loopback and no token is set.

    The server prints a clear error to stderr and exits non-zero — fail-loud.
    """
    env = _http_env(tmp_path, _free_port())
    env["MCP_HTTP_HOST"] = "0.0.0.0"
    env["MCP_HTTP_BEARER_TOKEN"] = ""

    proc = spawn_server(env)
    try:
        _, stderr_bytes = proc.communicate(timeout=BOOT_DEADLINE_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr_bytes = proc.communicate()
        pytest.fail("Server should have exited fast on loopback guard rejection")

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    assert proc.returncode != 0, f"Expected non-zero exit; got {proc.returncode}"
    assert "MCP_HTTP_BEARER_TOKEN" in stderr_text
    assert "0.0.0.0" in stderr_text


def test_http_jsonl_logs_have_no_bearer_token(tmp_path: Path) -> None:
    """After a real HTTP round-trip, scan JSONL logs for the bearer literal.

    Even though Phase 8 doesn't introduce new logging surfaces, this is the
    canary that catches a future contributor accidentally piping request
    headers into a Recorder field.
    """
    port = _free_port()
    env = _http_env(tmp_path, port)
    log_dir = tmp_path / "logs"

    proc = spawn_server(env)
    try:
        wait_for_ready(proc, BOOT_DEADLINE_SEC, ready_marker=READY_MARKER)
        _wait_for_listener(port)

        url = f"http://127.0.0.1:{port}/mcp"
        with httpx.Client(timeout=5.0) as client:
            client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "leak-canary", "version": "0.0.1"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {BEARER_TOKEN}",
                },
            )

        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=SHUTDOWN_GRACE_SEC)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    if not log_dir.exists():
        pytest.skip("No JSONL logs produced — nothing to scan.")
        return

    for jsonl in log_dir.rglob("*.jsonl"):
        for line in jsonl.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            # Ensure the line is valid JSON before searching — if a future
            # change writes raw text, this assert will catch it.
            json.loads(line)
            assert BEARER_TOKEN not in line, f"Bearer token literal leaked into {jsonl}:\n{line}"
