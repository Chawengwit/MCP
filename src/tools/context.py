from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.auth import Credentials
from src.config import ApiConfig
from src.events import Recorder
from src.gateway import GraphQLClient, RestClient


@dataclass
class ToolContext:
    """Dependency container passed to every Phase 5 tool handler.

    Built once at server startup; closures over this object hand each tool the
    config map, credential store, gateway factories, and Recorder it needs.
    Tests construct one with mock factories and a tmp-dir-backed Recorder.
    """

    configs: dict[str, ApiConfig]
    credentials: Credentials
    rest_client_factory: Callable[[ApiConfig], RestClient]
    graphql_client_factory: Callable[[ApiConfig], GraphQLClient]
    recorder: Recorder
