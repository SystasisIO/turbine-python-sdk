"""The service catalog keeps a traversal on the caller's deployment and revision."""

from __future__ import annotations

import inspect
import json
from functools import partial
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from turbine_sdk import (
    AsyncTurbine,
    CatalogSnapshotExpired,
    Forbidden,
    NotFound,
    Turbine,
    TurbineError,
    Unauthorized,
)
from turbine_sdk.schemas import (
    CatalogSnapshot,
    ComfyBinding,
    OpenAIBinding,
    Package,
    PackagePage,
    Support,
    UnsignedAuthor,
    VerifiedAuthor,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "native" / "package-catalog.json"
_CATALOG = "/api/broker/v1/catalog"
_PACKAGES = _CATALOG + "/packages"
_REVISION = "catalog/rev + ?&="
_ENCODED_REVISION = "catalog%2Frev%20%2B%20%3F%26%3D"
_CURSOR = "next/?&+=%"
_ENCODED_CURSOR = "next%2F%3F%26%2B%3D%25"
_PACKAGE_HASH = "b" * 64
_LIST = f"{_PACKAGES}?catalog_revision={_ENCODED_REVISION}&limit=100"
_GET = f"{_PACKAGES}/{_PACKAGE_HASH}?catalog_revision={_ENCODED_REVISION}"
_OMIT = object()


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text())


def _page(data: list[dict], cursor: str | None = None) -> dict:
    return {
        "catalog_revision": _REVISION,
        "data": data,
        "next_cursor": cursor,
        "has_more": cursor is not None,
    }


async def _result(value):
    return await value if inspect.isawaitable(value) else value


async def _close(client) -> None:
    if isinstance(client, (AsyncTurbine, httpx2.AsyncClient)):
        await client.aclose()
    else:
        client.close()


@pytest.fixture(params=[Turbine, AsyncTurbine], ids=["sync", "async"])
def client_type(request):
    return request.param


@pytest.fixture
async def packages(server, client_type):
    client = client_type(api_key="catalog-key")
    try:
        yield client.turbine.packages
    finally:
        await _close(client)


async def test_catalog_reads_one_revision_with_exact_paths_query_and_bearer(
    server, second_server, client_type
) -> None:
    fixture = _fixture()
    unsigned, verified = fixture["packages"]
    first = _page([unsigned], _CURSOR)
    last = _page([verified])
    prefix = "/some/deployment"
    first_path = f"{_PACKAGES}?catalog_revision={_ENCODED_REVISION}&limit=1"
    last_path = (
        f"{_PACKAGES}?catalog_revision={_ENCODED_REVISION}&cursor={_ENCODED_CURSOR}&limit=200"
    )
    replies = {_CATALOG: fixture["snapshot"], first_path: first, last_path: last, _GET: verified}
    server.state.package_catalog_replies = {prefix + path: body for path, body in replies.items()}
    pool_type = httpx2.AsyncClient if client_type is AsyncTurbine else httpx2.Client
    pool = pool_type(
        base_url=second_server.base_url,
        params={"catalog_revision": "wrong", "extraneous": "1"},
        follow_redirects=True,
        headers={"X-Shared-Pool": "present"},
        auth=("default", "auth"),
    )
    client = client_type(
        base_url=server.base_url + prefix + "/",
        router_base_url=second_server.base_url,
        api_key="catalog-key",
        client=pool,
    )
    try:
        packages = client.turbine.packages
        snapshot = await _result(packages.snapshot())
        assert isinstance(snapshot, CatalogSnapshot)
        assert snapshot.model_dump(mode="json") == fixture["snapshot"]
        page = await _result(packages.list(catalog_revision=snapshot.revision, limit=1))
        assert isinstance(page, PackagePage)
        assert page.model_dump(mode="json") == first
        assert isinstance(page.data[0].author, UnsignedAuthor)
        unmodeled = page.data[0].operations[0].bindings[0]
        assert isinstance(unmodeled, ComfyBinding)
        assert unmodeled.model_id is None and unmodeled.model_from_input is False
        tail = await _result(
            packages.list(catalog_revision=snapshot.revision, cursor=page.next_cursor, limit=200)
        )
        assert tail.model_dump(mode="json") == last
        detail = await _result(packages.get(_PACKAGE_HASH, catalog_revision=snapshot.revision))
        assert isinstance(detail, Package)
        assert detail == tail.data[0]
        assert isinstance(detail.author, VerifiedAuthor)
        assert detail.models[0].version is None
        fixed = detail.operations[0].bindings[0]
        assert fixed.model_id == "example-text" and fixed.model_from_input is False
        text, messages, chat = detail.operations[1].bindings
        assert text.class_type != messages.class_type and text.input_schema != messages.input_schema
        assert text.model_from_input is True and messages.model_id == fixed.model_id
        assert isinstance(chat, OpenAIBinding) and chat.api == "chat.completions"
    finally:
        await _close(client)
        assert not pool.is_closed
        await _close(pool)
    assert server.state.http_requests == [("GET", prefix + path) for path in replies]
    assert all(
        headers["Authorization"] == "Bearer catalog-key" for headers in server.state.native_headers
    )
    assert all(headers["X-Shared-Pool"] == "present" for headers in server.state.native_headers)
    assert second_server.state.http_requests == []


async def test_empty_catalog_with_default_limit(server, packages) -> None:
    server.state.package_catalog_replies[_LIST] = _page([])
    page = await _result(packages.list(catalog_revision=_REVISION))
    assert page.model_dump(mode="json") == _page([])
    assert server.state.http_requests == [("GET", _LIST)]


@pytest.mark.parametrize("destination", ["same-origin", "other-origin"])
async def test_catalog_does_not_follow_redirects(
    server, second_server, packages, destination
) -> None:
    target = "/api/object_info"
    if destination == "other-origin":
        target = second_server.base_url + target
    server.state.package_catalog_replies[_LIST] = _page([])
    server.state.native_status = 307
    server.state.native_response_headers = {"Location": target, "X-Request-Id": "redirect"}
    with pytest.raises(TurbineError) as caught:
        await _result(packages.list(catalog_revision=_REVISION))
    assert caught.value.http_status == 307
    assert caught.value.request_id == "redirect"
    assert server.state.http_requests == [("GET", _LIST)]
    assert second_server.state.http_requests == []


async def test_outdated_server_has_no_fallback_catalog(server, packages) -> None:
    with pytest.raises(NotFound) as caught:
        await _result(packages.snapshot())
    assert caught.value.http_status == 404
    assert server.state.http_requests == [("GET", _CATALOG)]


async def test_expired_revision_is_typed_for_both_reads_and_keeps_the_request_id(
    server, packages
) -> None:
    server.state.package_catalog_replies = {_LIST: {}, _GET: {}}
    server.state.native_status = 410
    server.state.native_body = {
        "request_id": "catalog-request",
        "code": "catalog_snapshot_expired",
        "message": "Catalog snapshot expired",
        "retryable": False,
        "details": {},
    }
    server.state.native_response_headers = {"X-Request-Id": "catalog-request"}
    for read in (
        lambda: packages.list(catalog_revision=_REVISION),
        lambda: packages.get(_PACKAGE_HASH, catalog_revision=_REVISION),
    ):
        with pytest.raises(CatalogSnapshotExpired) as caught:
            await _result(read())
        assert isinstance(caught.value, TurbineError)
        assert caught.value.http_status == 410 and caught.value.code == "catalog_snapshot_expired"
        assert caught.value.request_id == "catalog-request"
    assert server.state.http_requests == [("GET", _LIST), ("GET", _GET)]


@pytest.mark.parametrize(
    "status,code,error_class,retry_after",
    [
        (400, "invalid_request", TurbineError, None),
        (401, "unauthorized", Unauthorized, None),
        (403, "forbidden", Forbidden, None),
        (404, "not_found", NotFound, None),
        (410, "any_other_code", TurbineError, None),
        (429, "rate_limited", TurbineError, 7),
        (503, "service_unavailable", TurbineError, 11),
        (503, "catalog_snapshot_expired", TurbineError, None),
    ],
)
async def test_flat_catalog_errors_preserve_code_status_and_metadata(
    server, packages, status, code, error_class, retry_after
) -> None:
    server.state.package_catalog_replies[_LIST] = {}
    server.state.native_status = status
    server.state.native_body = {
        "request_id": "catalog-body-id",
        "code": code,
        "message": "Catalog refused request",
        "retryable": retry_after is not None,
        "details": {},
    }
    if retry_after is not None:
        server.state.native_response_headers = {"Retry-After": str(retry_after)}
    with pytest.raises(error_class) as caught:
        await _result(packages.list(catalog_revision=_REVISION))
    assert not isinstance(caught.value, CatalogSnapshotExpired)
    assert caught.value.http_status == status and caught.value.code == code
    assert caught.value.request_id == "catalog-body-id"
    assert caught.value.retry_after == retry_after
    assert caught.value.details == {}
    assert server.state.http_requests == [("GET", _LIST)]


async def test_catalog_error_does_not_echo_request_secrets(server, packages) -> None:
    server.state.package_catalog_replies[_CATALOG] = {}
    server.state.native_status = 503
    server.state.native_body = {
        "code": "service_unavailable",
        "request_id": "catalog-key",
        "message": "Authorization: Bearer catalog-key",
        "details": {"catalog-key": "catalog-key", "token": "second-secret"},
    }
    server.state.native_response_headers = {"X-Request-Id": "catalog-key"}
    with pytest.raises(TurbineError) as caught:
        await _result(packages.snapshot())
    error = caught.value
    rendered = repr((str(error), error.request_id, error.details, error.__cause__))
    assert "catalog-key" not in rendered and "second-secret" not in rendered


@pytest.mark.parametrize("kind", ["json", "revision", "limit", "digest", "pagination"])
async def test_catalog_response_must_match_the_request_and_page_schema(
    server, packages, kind
) -> None:
    fixture = _fixture()
    server.state.native_response_headers = {"X-Request-Id": "bad-catalog"}
    limit = 1 if kind == "limit" else 100
    if kind == "json":
        server.state.native_raw_body = b"{invalid"
    if kind == "digest":
        server.state.package_catalog_replies[_GET] = fixture["packages"][0]
        read = partial(packages.get, _PACKAGE_HASH, catalog_revision=_REVISION)
    else:
        page = _page(fixture["packages"])
        if kind == "revision":
            page["catalog_revision"] = "other-revision"
        if kind == "pagination":
            page["has_more"] = True
        path = f"{_PACKAGES}?catalog_revision={_ENCODED_REVISION}&limit={limit}"
        server.state.package_catalog_replies[path] = page
        read = partial(packages.list, catalog_revision=_REVISION, limit=limit)
    with pytest.raises(TurbineError) as caught:
        await _result(read())
    assert caught.value.code == (
        "invalid_json_response" if kind == "json" else "invalid_schema_response"
    )
    assert caught.value.http_status == 200 and caught.value.request_id == "bad-catalog"


@pytest.mark.parametrize(
    "field,value",
    [
        ("catalog_revision", ""),
        ("catalog_revision", "x" * 257),
        ("cursor", ""),
        ("cursor", 3),
        ("limit", 0),
        ("limit", 201),
        ("limit", True),
        ("limit", "2"),
        ("package_hash", "A" * 64),
        ("package_hash", "../other"),
    ],
)
async def test_invalid_catalog_parameters_are_rejected_before_request(
    server, packages, field, value
) -> None:
    with pytest.raises(ValueError, match=field):
        if field == "package_hash":
            await _result(packages.get(value, catalog_revision=_REVISION))
        else:
            args = {"catalog_revision": _REVISION, field: value}
            await _result(packages.list(**args))
    assert server.state.http_requests == []


def test_catalog_ignores_new_fields_and_preserves_the_json_schema() -> None:
    original = _fixture()["packages"][1]
    enriched = _fixture()["packages"][1]
    enriched["new_field"] = "future"
    enriched["reference"]["new_field"] = {}
    enriched["models"][0]["new_field"] = "future"
    enriched["operations"][0]["new_field"] = []
    enriched["operations"][0]["bindings"][0]["new_field"] = 1
    enriched["author"]["new_field"] = True
    enriched["support"]["new_field"] = None
    typed = Package.model_validate(enriched)
    assert typed.model_dump(mode="json") == original
    assert Package.model_validate_json(typed.model_dump_json()) == typed


@pytest.mark.parametrize("channel", ["url", "email"])
def test_support_may_have_one_contact_channel(channel) -> None:
    support = _fixture()["packages"][1]["support"]
    support[channel] = None
    assert Support.model_validate(support).model_dump(mode="json") == support


@pytest.mark.parametrize(
    "path,value",
    [
        (("descriptor_format",), "turbine::compute-package/1.0"),
        (("reference", "digest"), "B" * 64),
        (("models", 0, "version"), _OMIT),
        (("models", 0, "display_name"), ""),
        (("models",), []),
        (("models", 0, "id"), "example-image"),
        (("operations",), []),
        (("operations", 0, "id"), "text.chat"),
        (("operations", 0, "bindings"), []),
        (("operations", 0, "output_schema"), True),
        (("operations", 1, "bindings", 0, "id"), "messages"),
        (("operations", 0, "bindings", 0, "model_id"), _OMIT),
        (("operations", 0, "bindings", 0, "api"), None),
        (("operations", 0, "bindings", 0, "input_schema"), []),
        (("operations", 0, "bindings", 0, "protocol"), "another"),
        (("operations", 0, "bindings", 0, "class_type"), "界" * 100),
        (("operations", 0, "bindings", 0, "model_from_input"), 1),
        (("operations", 1, "bindings", 0, "model_id"), None),
        (("operations", 1, "bindings", 2, "model_id"), None),
        (("operations", 1, "bindings", 2, "api"), "another"),
        (("operations", 1, "bindings", 2, "model_from_input"), False),
        (("operations", 1, "bindings", 2, "class_type"), None),
        (("author", "status"), "unknown"),
        (("author", "status"), "unsigned"),
        (("author", "account"), "examplepub."),
        (("author", "verified_at_block_id"), _OMIT),
        (("support",), _OMIT),
        (("support",), {"url": None, "email": None}),
        (("support", "url"), "http://example.org"),
        (("support", "url"), "https://password@example.org"),
        (("support", "email"), "mailto:support@example.org"),
    ],
)
def test_package_rejects_invalid_fields_and_inconsistent_variants(path, value) -> None:
    package = _fixture()["packages"][1]
    target = package
    for key in path[:-1]:
        target = target[key]
    if value is _OMIT:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    with pytest.raises(ValidationError):
        Package.model_validate(package)


def test_models_must_all_be_referenced_by_visible_bindings() -> None:
    package = _fixture()["packages"][1]
    package["operations"].pop()
    with pytest.raises(ValidationError, match="models must match"):
        Package.model_validate(package)


@pytest.mark.parametrize(
    "kind", ["out-of-order", "duplicate", "empty-continuation", "missing-cursor"]
)
def test_page_cannot_contradict_its_order_and_continuation(kind) -> None:
    first, second = _fixture()["packages"]
    if kind == "out-of-order":
        page = _page([second, first])
    elif kind == "duplicate":
        page = _page([first, first])
    elif kind == "empty-continuation":
        page = _page([], _CURSOR)
    else:
        page = _page([])
        del page["next_cursor"]
    with pytest.raises(ValidationError):
        PackagePage.model_validate(page)


@pytest.mark.parametrize(
    "expires_at",
    ["2026-11-17T10:15:00+00:00", "2026-02-30T10:15:00Z", "2026-11-17T10:10:00.123456788Z", 0],
)
def test_snapshot_requires_valid_utc_window(expires_at) -> None:
    snapshot = _fixture()["snapshot"]
    with pytest.raises(ValidationError):
        CatalogSnapshot.model_validate({**snapshot, "expires_at": expires_at})
