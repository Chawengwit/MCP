"""Shared subprocess scaffolding for `tests/integration/`.

Both stdio (`test_smoke.py`) and HTTP (`test_http_smoke.py`) smoke tests spawn
`python -m src.server` and wait for a ready marker on stderr. The helpers
below are the shared skeleton; each test supplies its own marker, any
HTTP-specific env vars, and the assertions on the captured stderr.

This file is named `_helpers.py` (not `conftest.py`) because pytest's
`conftest.py` carries a fixture-module connotation — these are plain
helper functions, not auto-injected fixtures, so a non-conftest name is
clearer for future contributors. The leading underscore signals "private
to this test directory."
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Bounded waits so a hung server fails the test rather than the suite.
BOOT_DEADLINE_SEC = 8.0
SHUTDOWN_GRACE_SEC = 10.0

# Project root, used as cwd for the subprocess so module imports resolve.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def spawn_server(env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Spawn `python -m src.server` with stdin closed and stderr piped.

    PYTHONUNBUFFERED=1 prevents block-buffering of stderr through the pipe
    (defense-in-depth: the server uses sys.stderr.print which is line-buffered,
    but a future contributor switching to raw `print()` would otherwise hang
    `wait_for_ready` waiting for a buffer flush).
    """
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [sys.executable, "-m", "src.server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=PROJECT_ROOT,
    )


def isolated_env(tmp_path: Path) -> dict[str, str]:
    """Return a base env dict that isolates the subprocess from local config.

    Points MCP_API_CONFIG_PATH at a nonexistent file so `load_api_configs`
    returns an empty dict — keeps the test independent of the developer's
    working tree (which may have populated configs with unresolved ${VAR}
    placeholders that would crash the server before the ready marker).
    """
    env = os.environ.copy()
    env["MCP_LOG_DIR"] = str(tmp_path / "logs")
    env["MCP_LOG_BUFFER_SIZE"] = "1"
    env["MCP_API_CONFIG_PATH"] = str(tmp_path / "no_such_config.json")
    return env


def wait_for_ready(
    proc: subprocess.Popen[bytes],
    deadline_sec: float,
    *,
    ready_marker: str,
) -> bytes:
    """Read stderr line-by-line until `ready_marker` appears or we time out.

    `ready_marker` is keyword-only so call sites read as
    ``wait_for_ready(proc, BOOT_DEADLINE_SEC, ready_marker=READY_MARKER)`` —
    avoids the third positional being mistaken for a second timeout value.

    Returns the captured stderr fragment so tests can assert log content.
    Raises:
      - RuntimeError if the process exits before the marker (real boot failure)
      - TimeoutError if `deadline_sec` passes with the marker still absent
    """
    assert proc.stderr is not None
    captured = bytearray()
    marker_bytes = ready_marker.encode()
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Server exited before ready marker; returncode={proc.returncode}, "
                    f"stderr so far:\n{captured.decode(errors='replace')}"
                )
            time.sleep(0.05)
            continue
        captured.extend(line)
        if marker_bytes in line:
            return bytes(captured)
    raise TimeoutError(
        f"Ready marker {ready_marker!r} not seen within {deadline_sec}s. "
        f"Stderr so far:\n{captured.decode(errors='replace')}"
    )
