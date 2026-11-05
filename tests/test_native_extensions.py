"""Three native extensions use the existing client and never materialize assets."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest
from pydantic import ValidationError

from turbine_low.extension_transport import EXTENSION_ROUTES
from turbine_low.transport import AsyncTurbineLow, TurbineLow
from turbine_sdk import AsyncTurbine, Turbine, TurbineError
from turbine_sdk.schemas import EstimateReply, NodeCatalog, PromptGraph

_FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


async def _result(value):
    return await value if inspect.isawaitable(value) else value


async def _close(client) -> None:
    if isinstance(client, AsyncTurbine):
        await client.aclose()
    else:
        client.close()


@pytest.fixture(params=[Turbine, AsyncTurbine], ids=["sync", "async"])
def client_type(request):
    return request.param


@pytest.mark.parametrize("scheme", ["http", "HTTP"])
async def test_exact_routes_share_deployment_key_pool_and_preserve_single_node_mapping(
    server, client_type, scheme
) -> None:
    name = "Native /节点?x#%"
    server.state.native_deployment_prefix = "/deployment"
    server.state.native_catalog = _fixture("catalog.json")
    server.state.native_catalog[name] = {"category": None, "extra": ["preserved"]}
    server.state.native_estimate = _fixture("estimate-partial.json")
    graph = PromptGraph.model_validate(_fixture("graph.json"))
    borrowed_type = httpx2.AsyncClient if client_type is AsyncTurbine else httpx2.Client
    borrowed = borrowed_type(headers={"X-Shared-Pool": "configured"}, auth=("default", "auth"))
    client = client_type(
        base_url=server.base_url.replace("http://", scheme + "://") + "/deployment/",
        api_key="native-key",
        client=borrowed,
    )
    try:
        catalog = await _result(client.turbine.nodes.list())
        single = await _result(client.turbine.nodes.get(name))
        estimate = await _result(client.turbine.estimate_prompt(graph))
        assert isinstance(catalog, NodeCatalog)
        assert catalog.model_dump() == server.state.native_catalog
        assert single.model_dump() == {name: server.state.native_catalog[name]}
        assert isinstance(estimate, EstimateReply)
        assert estimate.model_dump() == server.state.native_estimate
        await _close(client)
        assert not borrowed.is_closed
        # The namespace still uses the borrowed connection pool after the SDK
        # context is closed; no extension-specific HTTP client exists.
        assert (await _result(client.turbine.nodes.get("Minimal"))).model_dump() == {
            "Minimal": {"output": []}
        }
    finally:
        await _close(client)
        if isinstance(borrowed, httpx2.AsyncClient):
            await borrowed.aclose()
        else:
            borrowed.close()
    assert server.state.native_paths == [
        "/deployment/api/object_info",
        "/deployment/api/object_info/" + quote(name, safe=""),
        "/deployment/api/prompt/estimate",
        "/deployment/api/object_info/Minimal",
    ]
    assert server.state.native_prompts == [{"prompt": graph.model_dump()}]
    assert all(
        headers["Authorization"] == "Bearer native-key" for headers in server.state.native_headers
    )
    assert all(headers["X-Shared-Pool"] == "configured" for headers in server.state.native_headers)
    assert all("/api/v2/" not in path for _, path in server.state.http_requests)
    assert server.state.upload_count == server.state.submit_count == 0


@pytest.mark.parametrize(
    "reply",
    [
        {"total": None, "nodes": [], "breakdown_complete": False},
        _fixture("estimate-partial.json"),
        {
            "total": {"amount": "125", "precision": 4, "unit": "USD"},
            "nodes": [
                {
                    "node_id": "1",
                    "model_id": "provider/example",
                    "price": {"amount": "125", "precision": 4, "unit": "USD"},
                }
            ],
            "breakdown_complete": True,
        },
    ],
    ids=["unknown", "partial", "complete"],
)
async def test_estimate_sends_only_the_prepared_graph_and_never_submits(
    server, client_type, reply
) -> None:
    server.state.native_estimate = reply
    raw = _fixture("graph.json")
    client = client_type(api_key="preview-key")
    try:
        result = await _result(client.turbine.estimate_prompt(raw))
    finally:
        await _close(client)
    assert result.model_dump(mode="json") == reply
    assert server.state.native_prompts == [{"prompt": raw}]
    assert server.state.http_requests == [("POST", "/api/prompt/estimate")]
    assert server.state.upload_count == server.state.submit_count == 0


@pytest.mark.parametrize("kind", ["handle", "workflow", "mutated_model", "constructed_model"])
async def test_unprepared_values_fail_before_any_http(server, client_type, kind) -> None:
    client = client_type()
    handle = client.assets.from_bytes(b"not-uploaded", filename="input.png")
    raw = {"1": {"class_type": "Example", "inputs": {"image": handle}}}
    if kind == "workflow":
        prompt = client.workflows.from_json(raw)
    elif kind == "mutated_model":
        prompt = PromptGraph.model_validate({"1": {"class_type": "Example", "inputs": {}}})
        prompt.root["1"].inputs["image"] = handle
    elif kind == "constructed_model":
        prompt = PromptGraph.model_construct(root=raw)
    else:
        prompt = raw
    try:
        with pytest.raises(ValidationError):
            await _result(client.turbine.estimate_prompt(prompt))
    finally:
        await _close(client)
    assert server.state.http_requests == []
    assert server.state.upload_count == server.state.submit_count == 0


@pytest.mark.parametrize("status", [400, 422, 429, 503])
async def test_native_error_keeps_status_type_details_and_retry_header(
    server, client_type, status
) -> None:
    server.state.native_status = status
    server.state.native_body = {
        "error": {
            "type": "native_prompt_invalid",
            "message": "Prompt rejected",
            "details": "unsupported input",
            "extra_info": {"field": "seed"},
        },
        "node_errors": {"1": {"errors": [{"type": "invalid_input", "message": "invalid seed"}]}},
    }
    server.state.native_response_headers = {
        "Retry-After": "7",
        "X-Comfy-Request-Id": "native_request",
    }
    client = client_type(api_key="native-private-key")
    try:
        with pytest.raises(TurbineError) as caught:
            await _result(client.turbine.estimate_prompt({}))
    finally:
        await _close(client)
    error = caught.value
    assert error.code == "native_prompt_invalid"
    assert error.http_status == status
    assert error.request_id == "native_request"
    assert error.retry_after == 7
    assert error.details["node_errors"] == server.state.native_body["node_errors"]
    assert error.details["extra_info"] == {"field": "seed"}
    assert error.details["details"] == "unsupported input"
    assert len(server.state.native_paths) == 1
    assert "native-private-key" not in str(error)


@pytest.mark.parametrize(
    "body",
    [
        {"detail": "Invalid native graph"},
        {"detail": [{"loc": ["body", "prompt"], "msg": "Field required", "type": "missing"}]},
    ],
)
async def test_native_detail_errors_remain_in_the_existing_hierarchy(
    server, client_type, body
) -> None:
    server.state.native_status = 422
    server.state.native_body = body
    client = client_type()
    try:
        with pytest.raises(TurbineError) as caught:
            await _result(client.turbine.nodes.list())
    finally:
        await _close(client)
    assert caught.value.http_status == 422
    assert "Field required" in str(caught.value) or "Invalid native graph" in str(caught.value)
    assert caught.value.__class__.__module__.startswith("turbine_sdk")


@pytest.mark.parametrize(
    "body",
    [
        {"detail": {"headers": {"Authorization": "Bearer native-private-key"}}},
        {"headers": {"Authorization": "Bearer native-private-key"}},
        {"error": {"type": "native_invalid", "message": "Rejected Bearer native-private-key"}},
        {"detail": [{"msg": "Bad native-private-key", "input": {"api_key": "another-secret"}}]},
    ],
)
async def test_native_errors_never_echo_credentials_or_secret_headers(
    server, client_type, body
) -> None:
    server.state.native_status = 422
    server.state.native_body = body
    client = client_type(api_key="native-private-key")
    try:
        with pytest.raises(TurbineError) as caught:
            await _result(client.turbine.nodes.list())
    finally:
        await _close(client)
    error = caught.value
    rendered = repr((str(error), repr(error), error.details, error.__cause__))
    assert "native-private-key" not in rendered
    assert "another-secret" not in rendered
    assert error.http_status == 422


@pytest.mark.parametrize("status", [404, 503])
async def test_unknown_non_json_errors_preserve_status_without_body_leaks(
    server, client_type, status
) -> None:
    server.state.native_status = status
    server.state.native_raw_body = b"<html>Authorization: Bearer native-private-key</html>"
    client = client_type(api_key="native-private-key")
    try:
        with pytest.raises(TurbineError) as caught:
            await _result(client.turbine.nodes.get("Example"))
    finally:
        await _close(client)
    assert caught.value.http_status == status
    assert caught.value.code == ("not_found" if status == 404 else "http_503")
    assert "native-private-key" not in str(caught.value)


@pytest.mark.parametrize(
    "body,expected",
    [
        (b"{broken", "invalid_json_response"),
        (b"null", "invalid_schema_response"),
        (b"[]", "invalid_schema_response"),
        (b'{"total":null,"nodes":[]}', "invalid_schema_response"),
        (b'{"total":null,"nodes":[],"breakdown_complete":1}', "invalid_schema_response"),
    ],
)
async def test_malformed_json_and_invalid_response_schemas_are_distinguishable(
    server, client_type, body, expected
) -> None:
    server.state.native_raw_body = body
    server.state.native_response_headers = {"X-Comfy-Request-Id": "bad_reply"}
    client = client_type()
    try:
        with pytest.raises(TurbineError) as caught:
            await _result(client.turbine.estimate_prompt({}))
    finally:
        await _close(client)
    assert caught.value.code == expected
    assert caught.value.http_status == 200
    assert caught.value.request_id == "bad_reply"


async def test_catalog_response_is_validated_too(server, client_type) -> None:
    server.state.native_body = {"Example": {"output": [123]}}
    client = client_type()
    try:
        with pytest.raises(TurbineError) as caught:
            await _result(client.turbine.nodes.list())
    finally:
        await _close(client)
    assert caught.value.code == "invalid_schema_response"


async def test_native_calls_use_the_client_timeout(server, client_type) -> None:
    server.state.native_delay = 0.15
    client = client_type(timeout=0.02)
    try:
        with pytest.raises(httpx2.TimeoutException):
            await _result(client.turbine.nodes.list())
    finally:
        await _close(client)
    assert len(server.state.native_paths) == 1


async def test_async_estimate_cancellation_propagates_without_submit(server) -> None:
    server.state.native_delay = 0.15
    async with AsyncTurbine() as client:
        task = asyncio.create_task(client.turbine.estimate_prompt({}))

        async def wait_for_request() -> None:
            while not server.state.native_paths:
                if task.done():
                    await task
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_request(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert server.state.http_requests == [("POST", "/api/prompt/estimate")]
    assert server.state.upload_count == server.state.submit_count == 0


@pytest.mark.parametrize("name", ["", ".", "..", None, 123])
async def test_invalid_node_names_are_rejected_before_http(server, client_type, name) -> None:
    client = client_type()
    try:
        with pytest.raises((TypeError, ValueError)):
            await _result(client.turbine.nodes.get(name))
    finally:
        await _close(client)
    assert server.state.http_requests == []


def test_extensions_have_separate_exact_route_coverage() -> None:
    assert EXTENSION_ROUTES == (
        ("GET", "/api/object_info"),
        ("GET", "/api/object_info/{class_type}"),
        ("POST", "/api/prompt/estimate"),
    )
    for transport in (TurbineLow, AsyncTurbineLow):
        for name in ("get_node_catalog", "get_node_definition", "post_prompt_estimate"):
            assert callable(getattr(transport, name))
