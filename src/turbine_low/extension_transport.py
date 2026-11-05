"""Sans-IO URL, payload and response helpers for the three native extensions."""

from __future__ import annotations

import re
from typing import Any, TypeVar
from urllib.parse import quote

import httpx2
from pydantic import BaseModel, ValidationError

from .errors import ApiError, clean_body_excerpt, error_from_envelope, summarise_detail
from .models.graph import PromptGraph
from .models.pricing import EstimateRequest

NODES_PATH = "/api/object_info"
ESTIMATE_PATH = "/api/prompt/estimate"
EXTENSION_ROUTES = (
    ("GET", NODES_PATH),
    ("GET", NODES_PATH + "/{class_type}"),
    ("POST", ESTIMATE_PATH),
)

_Model = TypeVar("_Model", bound=BaseModel)
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


class _AllNodes:
    pass


_ALL_NODES = _AllNodes()


def _request_secrets(resp: httpx2.Response) -> tuple[str, ...]:
    try:
        headers = resp.request.headers
    except RuntimeError:
        return ()
    secrets = []
    for name in ("Authorization", "Proxy-Authorization", "Cookie"):
        value = headers.get(name)
        if value:
            secrets.append(value)
            if name != "Cookie" and " " in value:
                secrets.append(value.split(" ", 1)[1])
    return tuple(secrets)


def _safe_text(value: Any, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    for secret in secrets:
        value = value.replace(secret, "[redacted]")
    return clean_body_excerpt(_BEARER.sub("Bearer [redacted]", value))


def node_catalog_url(base_url: str, class_type: str | _AllNodes = _ALL_NODES) -> str:
    """A native URL below the configured deployment, without the v2 prefix."""
    suffix = ""
    if class_type is not _ALL_NODES:
        if not isinstance(class_type, str):
            raise TypeError("class_type must be a string")
        if not class_type or class_type in (".", ".."):
            raise ValueError("class_type must name a node")
        suffix = "/" + quote(class_type, safe="")
    return base_url.rstrip("/") + NODES_PATH + suffix


def estimate_url(base_url: str) -> str:
    """The estimate route on the same deployment as v2 jobs and assets."""
    return base_url.rstrip("/") + ESTIMATE_PATH


def prepare_estimate(prompt: PromptGraph | dict[str, Any]) -> dict[str, Any]:
    """Validate the whole graph before HTTP, including an existing mutable model."""
    prepared = PromptGraph.model_validate(prompt)
    return EstimateRequest(prompt=prepared).model_dump(mode="json")


def _safe_details(value: Any, secrets: tuple[str, ...]) -> Any:
    """Keep native validation details without echoing credential-bearing keys."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if key.lower().replace("-", "_")
                in {"authorization", "api_key", "token", "password", "cookie", "set_cookie"}
                else _safe_details(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_details(item, secrets) for item in value]
    if isinstance(value, str):
        return _safe_text(value, secrets)
    return value


def _native_error(
    resp: httpx2.Response,
    body: Any,
    *,
    retry_after: int | None,
    request_id: str | None,
) -> ApiError:
    # Comfy's native error uses error.type, not the v2 error.code envelope.
    # A detail string/list is another native HTTP error shape, not evidence
    # that the response came from Router.
    error = body.get("error") if isinstance(body, dict) else None
    secrets = _request_secrets(resp)
    code: str | None = None
    message: str | None = None
    details: dict[str, Any] = {}
    if isinstance(error, dict):
        raw_code = _safe_text(error.get("type") or error.get("code"), secrets)
        if isinstance(raw_code, str) and _CODE.fullmatch(raw_code):
            code = raw_code
        message = _safe_text(error.get("message"), secrets)
        details = {
            key: _safe_details(error[key], secrets)
            for key in ("details", "extra_info")
            if key in error
        }
    elif isinstance(error, str):
        message = _safe_text(error, secrets)
    if isinstance(body, dict):
        if "node_errors" in body:
            details["node_errors"] = _safe_details(body["node_errors"], secrets)
        detail = body.get("detail")
        if message is None:
            message = _safe_text(detail, secrets) or _safe_text(summarise_detail(detail), secrets)
        if isinstance(detail, (dict, list)):
            details["detail"] = _safe_details(detail, secrets)
    if code is None:
        # Reuse the SDK's established status classification, without treating
        # native error.type or detail as a Router envelope.
        code = error_from_envelope(resp.status_code, None).code
    return ApiError(
        message or f"HTTP {resp.status_code} from native extension",
        code=code,
        http_status=resp.status_code,
        details=details or None,
        retry_after=retry_after,
        request_id=request_id,
    )


def parse_native_response(
    resp: httpx2.Response,
    model: type[_Model],
    *,
    retry_after: int | None = None,
    request_id: str | None = None,
) -> _Model:
    """Read native errors and distinguish invalid JSON from a wrong schema."""
    try:
        body = resp.json()
    except ValueError:
        if resp.status_code != 200:
            raise _native_error(
                resp, None, retry_after=retry_after, request_id=request_id
            ) from None
        raise ApiError(
            "Native extension returned malformed JSON",
            code="invalid_json_response",
            http_status=resp.status_code,
            request_id=request_id,
        ) from None
    if resp.status_code != 200:
        raise _native_error(resp, body, retry_after=retry_after, request_id=request_id)
    try:
        return model.model_validate(body)
    except ValidationError:
        # Do not chain Pydantic's input-bearing error or copy response values
        # into the exception. The route and expected model identify the fault.
        raise ApiError(
            f"Native extension response does not match {model.__name__}",
            code="invalid_schema_response",
            http_status=resp.status_code,
            request_id=request_id,
            details={"schema": model.__name__},
        ) from None


__all__ = [
    "ESTIMATE_PATH",
    "EXTENSION_ROUTES",
    "NODES_PATH",
    "estimate_url",
    "node_catalog_url",
    "parse_native_response",
    "prepare_estimate",
]
