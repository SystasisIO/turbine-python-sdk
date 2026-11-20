"""Sans-IO URL, payload and response helpers for TurbineOS extensions."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import quote, urlencode

import httpx2
from pydantic import BaseModel, TypeAdapter, ValidationError

from .errors import (
    ApiError,
    CatalogSnapshotExpired,
    clean_body_excerpt,
    clean_request_id,
    error_from_envelope,
    summarise_detail,
)
from .models._scalars import CanonicalUuid, ExecutionTargets, Sha256Digest
from .models.graph import PromptGraph
from .models.packages import CatalogLimit, CatalogToken
from .models.pricing import EstimateRequest, JobSettlement

NODES_PATH = "/api/object_info"
ESTIMATE_PATH = "/api/prompt/estimate"
JOB_SETTLEMENT_PATH = "/api/v2/jobs/{id}/settlement"
CATALOG_PATH = "/api/broker/v1/catalog"
PACKAGES_PATH = CATALOG_PATH + "/packages"
EXTENSION_ROUTES = (
    ("GET", NODES_PATH),
    ("GET", NODES_PATH + "/{class_type}"),
    ("POST", ESTIMATE_PATH),
    ("GET", JOB_SETTLEMENT_PATH),
    ("GET", CATALOG_PATH),
    ("GET", PACKAGES_PATH),
    ("GET", PACKAGES_PATH + "/{package_hash}"),
)

_Model = TypeVar("_Model", bound=BaseModel)
_Parameter = TypeVar("_Parameter")
_ErrorParser = Callable[[httpx2.Response, Any, int | None, str | None], ApiError]
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_TOKEN: TypeAdapter[str] = TypeAdapter(CatalogToken)
_LIMIT: TypeAdapter[int] = TypeAdapter(CatalogLimit)
_DIGEST: TypeAdapter[str] = TypeAdapter(Sha256Digest)
_JOB_ID: TypeAdapter[str] = TypeAdapter(CanonicalUuid)
_EXECUTION_TARGETS: TypeAdapter[list[str]] = TypeAdapter(ExecutionTargets)


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


def _redact_secrets(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[redacted]")
    return _BEARER.sub("Bearer [redacted]", value)


def _safe_text(value: Any, secrets: tuple[str, ...]) -> str | None:
    return clean_body_excerpt(_redact_secrets(value, secrets)) if isinstance(value, str) else None


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


def job_settlement_url(base_url: str, job_id: str) -> str:
    identifier = _validate_parameter("job_id", job_id, _JOB_ID)
    return base_url.rstrip("/") + JOB_SETTLEMENT_PATH.format(id=identifier)


def catalog_snapshot_url(base_url: str) -> str:
    return base_url.rstrip("/") + CATALOG_PATH


def _validate_parameter(name: str, value: object, adapter: TypeAdapter[_Parameter]) -> _Parameter:
    try:
        return adapter.validate_python(value, strict=True)
    except ValidationError:
        raise ValueError(f"Invalid {name}") from None


def catalog_page_url(
    base_url: str, *, catalog_revision: str, cursor: str | None, limit: int
) -> str:
    params = {"catalog_revision": _validate_parameter("catalog_revision", catalog_revision, _TOKEN)}
    if cursor is not None:
        params["cursor"] = _validate_parameter("cursor", cursor, _TOKEN)
    params["limit"] = str(_validate_parameter("limit", limit, _LIMIT))
    query = urlencode(params, quote_via=quote)
    return f"{base_url.rstrip('/')}{PACKAGES_PATH}?{query}"


def catalog_package_url(base_url: str, package_hash: str, *, catalog_revision: str) -> str:
    digest = _validate_parameter("package_hash", package_hash, _DIGEST)
    revision = _validate_parameter("catalog_revision", catalog_revision, _TOKEN)
    query = urlencode({"catalog_revision": revision}, quote_via=quote)
    return f"{base_url.rstrip('/')}{PACKAGES_PATH}/{digest}?{query}"


def prepare_execution_targets(targets: list[str]) -> list[str]:
    return _validate_parameter("partial_execution_targets", targets, _EXECUTION_TARGETS)


def prepare_estimate(
    prompt: PromptGraph | dict[str, Any], *, partial_execution_targets: list[str] | None = None
) -> dict[str, Any]:
    """Validate the whole graph before HTTP, including an existing mutable model."""
    prepared = PromptGraph.model_validate(prompt)
    return EstimateRequest(
        prompt=prepared, partial_execution_targets=partial_execution_targets
    ).model_dump(mode="json")


def _safe_details(value: Any, secrets: tuple[str, ...]) -> Any:
    """Keep native validation details without echoing credential-bearing keys."""
    if isinstance(value, dict):
        return {
            _redact_secrets(key, secrets): (
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


def _catalog_error(
    resp: httpx2.Response,
    body: Any,
    retry_after: int | None,
    request_id: str | None,
) -> ApiError:
    if not isinstance(body, dict):
        return _native_error(resp, body, retry_after, request_id)
    secrets = _request_secrets(resp)
    request_id = request_id or clean_request_id(_safe_text(body.get("request_id"), secrets))
    code = _safe_text(body.get("code"), secrets)
    if code is None or _CODE.fullmatch(code) is None:
        return _native_error(resp, body, retry_after, request_id)
    error_type = (
        CatalogSnapshotExpired
        if resp.status_code == 410 and code == CatalogSnapshotExpired.code
        else ApiError
    )
    details = body.get("details")
    return error_type(
        _safe_text(body.get("message"), secrets) or f"HTTP {resp.status_code} from package catalog",
        code=code,
        http_status=resp.status_code,
        request_id=request_id,
        retry_after=retry_after,
        details=_safe_details(details, secrets) if isinstance(details, dict) else None,
    )


def _job_settlement_error(
    resp: httpx2.Response,
    body: Any,
    retry_after: int | None,
    request_id: str | None,
) -> ApiError:
    error = body.get("error") if isinstance(body, dict) else None
    envelope = None
    if isinstance(error, dict):
        try:
            sanitized = _safe_details(error, _request_secrets(resp))
        except RecursionError:
            pass
        else:
            code = sanitized.get("code")
            if not isinstance(code, str) or _CODE.fullmatch(code) is None:
                sanitized.pop("code", None)
            envelope = {"error": sanitized}
    return error_from_envelope(
        resp.status_code, envelope, retry_after=retry_after, request_id=request_id
    )


def _parse_extension_response(
    resp: httpx2.Response,
    model: type[_Model],
    *,
    name: str,
    error_parser: _ErrorParser,
    retry_after: int | None,
    request_id: str | None,
    matches_request: Callable[[_Model], bool] | None = None,
) -> _Model:
    try:
        body = resp.json()
    except (ValueError, RecursionError):
        if resp.status_code != 200:
            raise error_parser(resp, None, retry_after, request_id) from None
        raise ApiError(
            f"{name} returned malformed JSON",
            code="invalid_json_response",
            http_status=resp.status_code,
            request_id=request_id,
        ) from None
    if resp.status_code != 200:
        raise error_parser(resp, body, retry_after, request_id)
    try:
        result = model.model_validate(body)
    except (ValidationError, RecursionError):
        pass
    else:
        if matches_request is None or matches_request(result):
            return result
    # The schema name identifies the fault without copying response values.
    raise ApiError(
        f"{name} response does not match {model.__name__}",
        code="invalid_schema_response",
        http_status=resp.status_code,
        request_id=request_id,
        details={"schema": model.__name__},
    )


def parse_native_response(
    resp: httpx2.Response,
    model: type[_Model],
    *,
    retry_after: int | None = None,
    request_id: str | None = None,
) -> _Model:
    """Read native errors and distinguish invalid JSON from a wrong schema."""
    return _parse_extension_response(
        resp,
        model,
        name="Native extension",
        error_parser=_native_error,
        retry_after=retry_after,
        request_id=request_id,
    )


def parse_catalog_response(
    resp: httpx2.Response,
    model: type[_Model],
    *,
    retry_after: int | None,
    matches_request: Callable[[_Model], bool] | None,
) -> _Model:
    request_id = clean_request_id(
        _safe_text(resp.headers.get("X-Request-Id"), _request_secrets(resp))
    )
    return _parse_extension_response(
        resp,
        model,
        name="Package catalog",
        error_parser=_catalog_error,
        retry_after=retry_after,
        request_id=request_id,
        matches_request=matches_request,
    )


def parse_job_settlement_response(
    resp: httpx2.Response,
    job_id: str,
    *,
    retry_after: int | None,
    request_id: str | None,
) -> JobSettlement:
    return _parse_extension_response(
        resp,
        JobSettlement,
        name="Job settlement",
        error_parser=_job_settlement_error,
        retry_after=retry_after,
        request_id=clean_request_id(_safe_text(request_id, _request_secrets(resp))),
        matches_request=lambda settlement: settlement.job_id == job_id,
    )


__all__ = [
    "CATALOG_PATH",
    "ESTIMATE_PATH",
    "EXTENSION_ROUTES",
    "JOB_SETTLEMENT_PATH",
    "NODES_PATH",
    "PACKAGES_PATH",
    "catalog_package_url",
    "catalog_page_url",
    "catalog_snapshot_url",
    "estimate_url",
    "job_settlement_url",
    "node_catalog_url",
    "parse_catalog_response",
    "parse_job_settlement_response",
    "parse_native_response",
    "prepare_estimate",
    "prepare_execution_targets",
]
