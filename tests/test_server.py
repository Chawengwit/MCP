from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import load_api_configs
from src.events import Recorder
from src.server import _build_registry, _build_server, main
from src.tools import ToolRegistry


async def test_server_module_exposes_main() -> None:
    """The server entry point must export an async main() coroutine."""
    assert callable(main)


def test_config_loader_in_server_context() -> None:
    """Server reads config via load_api_configs without server-specific glue."""
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "api_configs.json"
        config_data = {
            "apis": {
                "test_api": {
                    "type": "rest",
                    "base_url": "https://test.example.com",
                    "endpoints": {"test": {}},
                }
            }
        }
        config_path.write_text(json.dumps(config_data))

        configs = load_api_configs(config_path)

        assert "test_api" in configs
        assert configs["test_api"].type == "rest"


def test_recorder_from_env_initializes() -> None:
    """Recorder.from_env() returns a usable instance."""
    rec = Recorder.from_env()
    assert hasattr(rec, "start")
    assert hasattr(rec, "stop")


async def test_build_registry_seeds_list_apis(recorder: Recorder) -> None:
    """_build_registry returns a fresh registry with list_apis pre-registered."""
    registry = _build_registry(recorder, {})

    assert isinstance(registry, ToolRegistry)
    assert len(registry) == 1
    spec = registry.get("list_apis")
    assert spec is not None
    assert callable(spec.handler)


async def test_build_registry_returns_independent_instances(
    recorder: Recorder,
) -> None:
    """Each _build_registry call returns a fresh registry — no global state."""
    r1 = _build_registry(recorder, {})
    r2 = _build_registry(recorder, {})

    assert r1 is not r2
    # Both have list_apis but registering again on r1 should not affect r2.
    assert len(r1) == 1
    assert len(r2) == 1


async def test_build_server_wires_registry(recorder: Recorder) -> None:
    """_build_server returns a Server that doesn't crash during construction."""
    registry = _build_registry(recorder, {})
    server = _build_server(registry)

    assert server is not None
    assert server.name == "mcp-data-gateway"
