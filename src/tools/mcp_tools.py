from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from src.auth import AuthRequiredError, CredentialStorageError
from src.config import ApiConfig
from src.events.redaction import redact_body
from src.gateway.handlers import (
    normalize_graphql_response,
    normalize_rest_response,
)
from src.tools.auth_resolver import (
    UnknownAuthTypeError,
    ensure_service_session,
    peek_auth_state,
    resolve_auth_headers,
)
from src.tools.context import ToolContext

logger = logging.getLogger("mcp.tools")


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class FetchDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: str
    endpoint: str
    filters: dict[str, Any] | None = None


class SendDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: str
    endpoint: str
    payload: dict[str, Any]


class GraphQLInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: str
    query: str
    variables: dict[str, Any] | None = None
    operation_name: str | None = None


class GetStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: str | None = None  # None → all configured APIs


# ---------------------------------------------------------------------------
# Public tool handlers
# ---------------------------------------------------------------------------


async def fetch_data(input_: FetchDataInput, *, context: ToolContext) -> dict[str, Any]:
    """GET data from a configured REST API."""
    session_id = uuid4()
    start_perf = time.perf_counter()
    args = input_.model_dump()
    result = "error"

    try:
        config = _resolve_config(input_.api_id, context)
        if config is None:
            return _api_not_configured_error(input_.api_id)

        if config.type != "rest":
            return _build_error(
                code="VALIDATION_ERROR",
                message=f"fetch_data requires a REST API; '{input_.api_id}' is type '{config.type}'",
                details={"api_id": input_.api_id},
            )

        endpoint_cfg = config.endpoints.get(input_.endpoint)
        if endpoint_cfg is None:
            return _endpoint_not_found_error(input_.api_id, input_.endpoint)

        try:
            service_session = await ensure_service_session(config=config, context=context)
            headers = await resolve_auth_headers(
                config=config,
                api_id=input_.api_id,
                credentials=context.credentials,
                service_session=service_session,
            )
        except (AuthRequiredError, CredentialStorageError) as exc:
            return _auth_required_error(input_.api_id, exc)
        except UnknownAuthTypeError as exc:
            return _build_error(
                code="VALIDATION_ERROR",
                message=str(exc),
                details={"api_id": input_.api_id},
            )

        method = (endpoint_cfg.method or "GET").upper()
        path = endpoint_cfg.path or ""

        client = context.rest_client_factory(config)
        started_at = time.time()
        response = await client.request(method, path, params=input_.filters, headers=headers)
        result_dict = normalize_rest_response(
            api_id=input_.api_id,
            endpoint=input_.endpoint,
            response=response,
            started_at=started_at,
        )
        result = "success" if "error" not in result_dict else "error"
        return result_dict
    finally:
        await _record_invocation(
            context.recorder,
            session_id=session_id,
            user_id=context.user_id,
            tool="fetch_data",
            api=input_.api_id,
            endpoint=input_.endpoint,
            result=result,
            duration_ms=int((time.perf_counter() - start_perf) * 1000),
            args=args,
        )


async def send_data(input_: SendDataInput, *, context: ToolContext) -> dict[str, Any]:
    """POST/PUT data to a configured REST API."""
    session_id = uuid4()
    start_perf = time.perf_counter()
    args = input_.model_dump()
    result = "error"

    try:
        config = _resolve_config(input_.api_id, context)
        if config is None:
            return _api_not_configured_error(input_.api_id)

        if config.type != "rest":
            return _build_error(
                code="VALIDATION_ERROR",
                message=f"send_data requires a REST API; '{input_.api_id}' is type '{config.type}'",
                details={"api_id": input_.api_id},
            )

        endpoint_cfg = config.endpoints.get(input_.endpoint)
        if endpoint_cfg is None:
            return _endpoint_not_found_error(input_.api_id, input_.endpoint)

        try:
            service_session = await ensure_service_session(config=config, context=context)
            headers = await resolve_auth_headers(
                config=config,
                api_id=input_.api_id,
                credentials=context.credentials,
                service_session=service_session,
            )
        except (AuthRequiredError, CredentialStorageError) as exc:
            return _auth_required_error(input_.api_id, exc)
        except UnknownAuthTypeError as exc:
            return _build_error(
                code="VALIDATION_ERROR",
                message=str(exc),
                details={"api_id": input_.api_id},
            )

        method = (endpoint_cfg.method or "POST").upper()
        path = endpoint_cfg.path or ""

        client = context.rest_client_factory(config)
        started_at = time.time()
        response = await client.request(method, path, json=input_.payload, headers=headers)
        result_dict = normalize_rest_response(
            api_id=input_.api_id,
            endpoint=input_.endpoint,
            response=response,
            started_at=started_at,
        )
        result = "success" if "error" not in result_dict else "error"
        return result_dict
    finally:
        await _record_invocation(
            context.recorder,
            session_id=session_id,
            user_id=context.user_id,
            tool="send_data",
            api=input_.api_id,
            endpoint=input_.endpoint,
            result=result,
            duration_ms=int((time.perf_counter() - start_perf) * 1000),
            args=args,
        )


async def execute_graphql(input_: GraphQLInput, *, context: ToolContext) -> dict[str, Any]:
    """Execute a GraphQL query/mutation against a configured GraphQL API."""
    session_id = uuid4()
    start_perf = time.perf_counter()
    args = input_.model_dump()
    result = "error"

    try:
        config = _resolve_config(input_.api_id, context)
        if config is None:
            return _api_not_configured_error(input_.api_id)

        if config.type != "graphql":
            return _build_error(
                code="VALIDATION_ERROR",
                message=(
                    f"execute_graphql requires a GraphQL API; "
                    f"'{input_.api_id}' is type '{config.type}'"
                ),
                details={"api_id": input_.api_id},
            )

        try:
            service_session = await ensure_service_session(config=config, context=context)
            headers = await resolve_auth_headers(
                config=config,
                api_id=input_.api_id,
                credentials=context.credentials,
                service_session=service_session,
            )
        except (AuthRequiredError, CredentialStorageError) as exc:
            return _auth_required_error(input_.api_id, exc)
        except UnknownAuthTypeError as exc:
            return _build_error(
                code="VALIDATION_ERROR",
                message=str(exc),
                details={"api_id": input_.api_id},
            )

        client = context.graphql_client_factory(config)
        started_at = time.time()
        response = await client.execute(
            input_.query,
            variables=input_.variables,
            operation_name=input_.operation_name,
            headers=headers,
        )
        result_dict = normalize_graphql_response(
            api_id=input_.api_id, response=response, started_at=started_at
        )
        result = "success" if "error" not in result_dict else "error"
        return result_dict
    finally:
        await _record_invocation(
            context.recorder,
            session_id=session_id,
            user_id=context.user_id,
            tool="execute_graphql",
            api=input_.api_id,
            endpoint=None,
            result=result,
            duration_ms=int((time.perf_counter() - start_perf) * 1000),
            args=args,
        )


async def get_status(input_: GetStatusInput, *, context: ToolContext) -> dict[str, Any]:
    """Report auth state per configured API. Strictly read-only — no OAuth flow."""
    session_id = uuid4()
    start_perf = time.perf_counter()
    args = input_.model_dump()
    result = "success"

    try:
        target_ids: list[str]
        if input_.api_id is not None:
            if input_.api_id not in context.configs:
                return _api_not_configured_error(input_.api_id)
            target_ids = [input_.api_id]
        else:
            target_ids = list(context.configs.keys())

        items: list[dict[str, Any]] = []
        for api_id in target_ids:
            config = context.configs[api_id]
            # Branch matrix lives in src/tools/auth_resolver.py — sharing it with
            # resolve_auth_headers prevents drift if a new auth.type is added.
            state, expires_at = await peek_auth_state(
                config=config, api_id=api_id, credentials=context.credentials
            )
            entry: dict[str, Any] = {
                "api_id": api_id,
                "type": config.type,
                "auth_state": state,
            }
            if expires_at is not None:
                entry["expires_at"] = expires_at
            items.append(entry)

        return {
            "data": items,
            "metadata": {
                "source": "mcp_data_gateway",
                "endpoint": "get_status",
                "timestamp": _iso_utc_now(),
                "duration_ms": int((time.perf_counter() - start_perf) * 1000),
            },
        }
    except Exception as exc:
        result = "error"
        logger.warning("get_status failed: %s", type(exc).__name__)
        return _build_error(
            code="VALIDATION_ERROR",
            message=f"get_status failed: {type(exc).__name__}",
            details={},
        )
    finally:
        await _record_invocation(
            context.recorder,
            session_id=session_id,
            user_id=context.user_id,
            tool="get_status",
            api=input_.api_id,
            endpoint=None,
            result=result,
            duration_ms=int((time.perf_counter() - start_perf) * 1000),
            args=args,
        )


# ---------------------------------------------------------------------------
# Server-facing wrappers — parse raw JSON args via Pydantic, convert
# ValidationError → VALIDATION_ERROR response. The server registers these.
# ---------------------------------------------------------------------------


async def fetch_data_handler(arguments: dict[str, Any], *, context: ToolContext) -> dict[str, Any]:
    try:
        parsed = FetchDataInput(**arguments)
    except ValidationError as exc:
        return _validation_error(exc)
    return await fetch_data(parsed, context=context)


async def send_data_handler(arguments: dict[str, Any], *, context: ToolContext) -> dict[str, Any]:
    try:
        parsed = SendDataInput(**arguments)
    except ValidationError as exc:
        return _validation_error(exc)
    return await send_data(parsed, context=context)


async def execute_graphql_handler(
    arguments: dict[str, Any], *, context: ToolContext
) -> dict[str, Any]:
    try:
        parsed = GraphQLInput(**arguments)
    except ValidationError as exc:
        return _validation_error(exc)
    return await execute_graphql(parsed, context=context)


async def get_status_handler(arguments: dict[str, Any], *, context: ToolContext) -> dict[str, Any]:
    try:
        parsed = GetStatusInput(**arguments)
    except ValidationError as exc:
        return _validation_error(exc)
    return await get_status(parsed, context=context)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_config(api_id: str, context: ToolContext) -> ApiConfig | None:
    return context.configs.get(api_id)


def _api_not_configured_error(api_id: str) -> dict[str, Any]:
    return _build_error(
        code="API_NOT_CONFIGURED",
        message=f"API '{api_id}' is not in api_configs.json",
        details={"api_id": api_id},
    )


def _endpoint_not_found_error(api_id: str, endpoint: str) -> dict[str, Any]:
    return _build_error(
        code="ENDPOINT_NOT_FOUND",
        message=f"Endpoint '{endpoint}' not defined for API '{api_id}'",
        details={"api_id": api_id, "endpoint": endpoint},
    )


def _auth_required_error(
    api_id: str, exc: AuthRequiredError | CredentialStorageError
) -> dict[str, Any]:
    # Use the exception class name only, not its message — error messages may
    # contain env var names that we don't want to leak to MCP clients.
    return _build_error(
        code="AUTH_REQUIRED",
        message=f"Authentication required for '{api_id}'",
        details={"api_id": api_id, "reason": type(exc).__name__},
    )


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    field_errors = [
        {
            "field": ".".join(str(p) for p in err["loc"]) or "<root>",
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    return _build_error(
        code="VALIDATION_ERROR",
        message="Tool input failed validation",
        details={"field_errors": field_errors},
    )


def _build_error(*, code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


async def _record_invocation(
    recorder: Any,
    *,
    session_id: UUID,
    tool: str,
    api: str | None,
    endpoint: str | None,
    result: str,
    duration_ms: int,
    args: dict[str, Any],
    user_id: str | None = None,
) -> None:
    """Emit the audit + usage + insight triple for one tool call.

    `tool_args` is run through `redact_body` before record_insight so secret
    fields in user-supplied payloads (e.g. send_data's `payload={"api_key": ...}`)
    do not appear in the insight log.
    """
    redacted_args = redact_body(args)
    await recorder.record_audit(
        session_id=session_id,
        user_id=user_id,
        tool=tool,
        api=api,
        endpoint=endpoint,
        result=result,
        duration_ms=duration_ms,
    )
    await recorder.record_usage(
        tool=tool,
        user_id=user_id,
        api=api,
        endpoint=endpoint,
        status=result,
        duration_ms=duration_ms,
    )
    await recorder.record_insight(
        session_id=session_id,
        user_id=user_id,
        tool=tool,
        api=api,
        tool_args=redacted_args,
    )


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
