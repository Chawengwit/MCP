from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from src.config import ApiAuthConfig, ApiConfig, load_api_configs
from src.events import Recorder
from src.server import _build_registry, _build_server, _build_tool_context, main
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


async def test_build_registry_seeds_all_tools(recorder: Recorder) -> None:
    """_build_registry returns a fresh registry with list_apis + Phase 5 tools pre-registered."""
    context = _build_tool_context(api_configs={}, recorder=recorder)
    registry = _build_registry(context)

    assert isinstance(registry, ToolRegistry)
    expected = {"list_apis", "fetch_data", "send_data", "execute_graphql", "get_status"}
    assert {spec.name for spec in registry.all().values()} == expected
    for name in expected:
        spec = registry.get(name)
        assert spec is not None
        assert callable(spec.handler)


async def test_build_registry_returns_independent_instances(
    recorder: Recorder,
) -> None:
    """Each _build_registry call returns a fresh registry — no global state."""
    ctx = _build_tool_context(api_configs={}, recorder=recorder)
    r1 = _build_registry(ctx)
    r2 = _build_registry(ctx)

    assert r1 is not r2
    assert len(r1) == len(r2)
    assert {s.name for s in r1.all().values()} == {s.name for s in r2.all().values()}


async def test_build_server_wires_registry(recorder: Recorder) -> None:
    """_build_server returns a Server that doesn't crash during construction."""
    ctx = _build_tool_context(api_configs={}, recorder=recorder)
    registry = _build_registry(ctx)
    server = _build_server(registry)

    assert server is not None
    assert server.name == "mcp-data-gateway"


# ---------------------------------------------------------------------------
# _build_oauth_configs — validates required fields, skips invalid configs
# ---------------------------------------------------------------------------


def _make_oauth_api(
    *,
    client_id: str | None = "cid",
    client_secret: str | None = "csec",
    authorize_url: str | None = "https://provider.example.com/oauth/authorize",
    token_url: str | None = "https://provider.example.com/oauth/token",
) -> ApiConfig:
    return ApiConfig(
        type="rest",
        base_url="https://api.example.com",
        auth=ApiAuthConfig(
            type="oauth2",
            provider="example",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=authorize_url,
            token_url=token_url,
            scopes=["read"],
        ),
        endpoints={},
    )


def test_build_oauth_configs_happy_path() -> None:
    from src.server import _build_oauth_configs

    result = _build_oauth_configs({"good": _make_oauth_api()})
    assert "good" in result
    assert result["good"].client_id == "cid"
    assert result["good"].client_secret == "csec"


@pytest.mark.parametrize(
    "kwarg, value, missing_token",
    [
        ("client_id", None, "client_id"),
        ("client_id", "", "client_id"),
        ("client_secret", None, "client_secret"),
        ("client_secret", "", "client_secret"),
        ("authorize_url", None, "authorize_url"),
        ("token_url", None, "token_url"),
    ],
)
def test_build_oauth_configs_skips_when_required_field_missing(
    kwarg: str,
    value: str | None,
    missing_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.server import _build_oauth_configs

    api = _make_oauth_api(**{kwarg: value})
    result = _build_oauth_configs({"bad": api})
    assert "bad" not in result
    captured = capsys.readouterr()
    assert "bad" in captured.err
    assert missing_token in captured.err


def test_build_oauth_configs_ignores_non_oauth2_apis() -> None:
    """Bearer- and null-auth APIs are not OAuth2; they must not appear in the output."""
    from src.server import _build_oauth_configs

    apis = {
        "bearer_api": ApiConfig(
            type="rest",
            base_url="https://b.example.com",
            auth=ApiAuthConfig(type="bearer", token_env="X"),
            endpoints={},
        ),
        "noauth_api": ApiConfig(
            type="rest", base_url="https://n.example.com", auth=None, endpoints={}
        ),
        "oauth_api": _make_oauth_api(),
    }
    result = _build_oauth_configs(apis)
    assert set(result.keys()) == {"oauth_api"}
