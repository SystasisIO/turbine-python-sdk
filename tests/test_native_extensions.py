"""Turbine extensions share the client's transport and deployment."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest
from pydantic import BaseModel, ValidationError

from turbine_low.extension_transport import EXTENSION_ROUTES
from turbine_low.transport import AsyncTurbineLow, TurbineLow
from turbine_sdk import (
    AsyncTurbine,
    IdempotencyKeyReuse,
    NotFound,
    Turbine,
    TurbineError,
    Unauthorized,
    WorkflowFormatUi,
)
from turbine_sdk.schemas import (
    EstimateReply,
    EstimateRequest,
    JobSettlement,
    NodeCatalog,
    PricingAuthorization,
    PromptGraph,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "native"
_SUBMISSION_KEY = "aaaaaaaa-1234-4def-89ab-0123456789ab"
_JOB_ID = "bbbbbbbb-1234-4def-89ab-0123456789ab"
_PAID_GRAPH = {"1": {"class_type": "Example", "inputs": {"text": "hello"}}}
_OMIT = object()


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def _paid_estimate(amount: str, version: str) -> dict:
    money = {"amount": amount, "precision": 0, "unit": "CRED"}
    maximum = {**money, "contract": "cred.token"}
    node = {
        "node_id": "1",
        "package_hash": "1" * 64,
        "operation_id": "text.complete",
        "model_id": None,
    }
    return {
        "total": money,
        "nodes": [{**node, "price": money}],
        "breakdown_complete": True,
        "x-turbine": {
            "pricing": {
                "maximum": maximum,
                "nodes": [
                    {
                        **node,
                        "binding_id": "prompt",
                        "chain_id": "2" * 64,
                        "service": "18446744073709551615",
                        "version": version,
                        "terms_hash": version * 64,
                        "binding_digest": amount[0] * 64,
                        "maximum": maximum,
                    }
                ],
            }
        },
    }


def _settlement(status: str = "final", amount: str | None = "9007199254740993") -> dict:
    reply: dict = {"job_id": _JOB_ID, "status": status, "total": None, "nodes": []}
    if status == "final":
        assert amount is not None
        pricing = _paid_estimate(amount, "1")["x-turbine"]["pricing"]
        terms = pricing["nodes"][0]
        charge = terms.pop("maximum")
        reply["total"] = pricing["maximum"].copy()
        reply["nodes"] = [
            {**terms, "charge": charge, "outcome": "settled" if amount != "0" else "released"},
            {**terms, "node_id": "2", "charge": {**charge, "amount": "0"}, "outcome": "zero_price"},
            {**terms, "node_id": "3", "charge": {**charge, "amount": "0"}, "outcome": "not_opened"},
        ]
    return reply


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


@pytest.mark.parametrize("status", [302, 307])
@pytest.mark.parametrize("borrow_client", [False, True])
async def test_paid_submit_does_not_follow_redirect(server, client_type, status, borrow_client):
    server.state.jobs_redirect_once = (status, "/api/v2/jobs")
    borrowed_type = httpx2.AsyncClient if client_type is AsyncTurbine else httpx2.Client
    borrowed = borrowed_type(follow_redirects=True) if borrow_client else None
    client = client_type(api_key="owner-key", client=borrowed)
    try:
        workflow = client.workflows.from_json(_PAID_GRAPH)
        pricing = EstimateReply.model_validate(_paid_estimate("7", "1")).pricing
        with pytest.raises(TurbineError) as redirect:
            await _result(client.submit(workflow, idempotency_key=_SUBMISSION_KEY, pricing=pricing))
        assert redirect.value.http_status == status
        assert redirect.value.idempotency_key == _SUBMISSION_KEY
        assert redirect.value.envelope_code == "payment_unavailable"
        if borrowed is not None:
            assert borrowed.follow_redirects
    finally:
        await _close(client)
        if isinstance(borrowed, httpx2.AsyncClient):
            await borrowed.aclose()
        elif borrowed is not None:
            borrowed.close()
    assert server.state.http_requests == [("POST", "/api/v2/jobs")]
    assert server.state.jobs_idempotency_keys == [_SUBMISSION_KEY]
    assert server.state.idempotency == {}


async def test_unpriced_submit_preserves_default_redirect_behavior(server, client_type):
    server.state.jobs_redirect_once = (307, "/api/v2/jobs")
    client = client_type()
    try:
        workflow = client.workflows.from_json(_PAID_GRAPH)
        job = await _result(client.submit(workflow))
        assert job.id
    finally:
        await _close(client)
    assert server.state.http_requests == [("POST", "/api/v2/jobs")] * 2


async def test_submit_accepts_api_node_ids_named_like_ui_fields(server, client_type):
    graph = {
        "nodes": {"class_type": "Example", "inputs": {"text": "ready"}},
        "links": {"class_type": "Example", "inputs": {"value": ["nodes", 0]}},
        "last_node_id": {"class_type": "Example", "inputs": {"value": ["links", 0]}},
    }
    client = client_type()
    try:
        workflow = client.workflows.from_json(graph)
        job = await _result(client.submit(workflow))
        assert job.id
    finally:
        await _close(client)
    assert server.state.last_workflow == graph
    assert server.state.http_requests == [("POST", "/api/v2/jobs")]


async def test_submit_rejects_ui_nodes_and_links_arrays_before_writing(server, client_type):
    graph = {
        "nodes": [{"id": 1, "type": "Example"}],
        "links": [],
        "last_node_id": 1,
    }
    client = client_type()
    try:
        workflow = client.workflows.from_json(graph)
        with pytest.raises(WorkflowFormatUi):
            await _result(client.submit(workflow))
    finally:
        await _close(client)
    assert not server.state.http_requests


async def test_paid_submit_can_surface_queue_full_without_retrying(server, client_type):
    server.state.queue_full_times = 1
    server.state.native_estimate = _paid_estimate("7", "1")
    client = client_type()
    try:
        workflow = client.workflows.from_json(_PAID_GRAPH)
        pricing = (await _result(client.turbine.estimate_prompt(workflow.json))).pricing
        with pytest.raises(TurbineError) as refusal:
            await _result(
                client.submit(
                    workflow,
                    idempotency_key=_SUBMISSION_KEY,
                    pricing=pricing,
                    retry_queue_full=False,
                )
            )
        assert refusal.value.http_status == 429
        assert refusal.value.code == "queue_full"
        assert refusal.value.envelope_code == "queue_full"
        assert refusal.value.idempotency_key == _SUBMISSION_KEY
        assert server.state.queue_full_times == 0
        assert server.state.idempotency == {}
    finally:
        await _close(client)


@pytest.mark.parametrize(
    ("status", "amount"),
    [("pending", None), ("not_applicable", None), ("final", "0"), ("final", "9007199254740993")],
)
async def test_settlement_state_uses_the_owner_and_configured_deployment(
    server, client_type, status, amount
) -> None:
    server.state.native_deployment_prefix = "/deployment"
    server.state.require_auth = True
    body = _settlement(status, amount)
    server.state.job_settlement = body
    borrowed_type = httpx2.AsyncClient if client_type is AsyncTurbine else httpx2.Client
    borrowed = borrowed_type(
        auth=("other", "credential"),
        headers={"X-Shared-Pool": "configured"},
        params={"unrelated": "value"},
    )
    client = client_type(
        base_url=server.base_url + "/deployment/", api_key="owner-key", client=borrowed
    )
    try:
        settlement = await _result(client.turbine.job_settlement(_JOB_ID))
        assert isinstance(settlement, JobSettlement)
        assert settlement.job_id == _JOB_ID
        assert settlement.status == status
        assert settlement.model_dump(mode="json") == body
        if amount is None:
            assert settlement.total is None and settlement.nodes == []
        else:
            assert settlement.total is not None and settlement.total.amount == amount
            assert settlement.total.unit == "CRED" and settlement.total.contract == "cred.token"
        await _close(client)
        assert not borrowed.is_closed
    finally:
        await _close(client)
        if isinstance(borrowed, httpx2.AsyncClient):
            await borrowed.aclose()
        else:
            borrowed.close()
    assert server.state.http_requests == [("GET", f"/deployment/api/v2/jobs/{_JOB_ID}/settlement")]
    assert server.state.native_headers[0]["X-Shared-Pool"] == "configured"
    assert server.state.last_auth_header == "Bearer owner-key"
    assert server.state.submit_count == server.state.job_poll_count == 0


@pytest.mark.parametrize(
    "job_id", ["", "abc", "..", "https://example.invalid", None, _JOB_ID.upper()]
)
async def test_settlement_rejects_noncanonical_job_ids_before_http(
    server, client_type, job_id
) -> None:
    client = client_type()
    try:
        with pytest.raises(ValueError, match="job_id"):
            await _result(client.turbine.job_settlement(job_id))
    finally:
        await _close(client)
    assert server.state.http_requests == []


async def test_settlement_unavailable_and_wrong_job_do_not_produce_a_charge(
    server, client_type
) -> None:
    server.state.require_auth = True
    client = client_type(api_key="owner-key")
    anonymous = client_type(api_key=None)
    try:
        with pytest.raises(Unauthorized) as unauthenticated:
            await _result(anonymous.turbine.job_settlement(_JOB_ID))
        assert unauthenticated.value.http_status == 401
        with pytest.raises(NotFound) as missing:
            await _result(client.turbine.job_settlement(_JOB_ID))
        assert missing.value.http_status == 404 and missing.value.code == "job_not_found"
        server.state.job_settlement = {**_settlement(), "job_id": _SUBMISSION_KEY}
        with pytest.raises(TurbineError) as mismatched:
            await _result(client.turbine.job_settlement(_JOB_ID))
        assert mismatched.value.code == "invalid_schema_response"
        server.state.native_raw_body = b"{"
        with pytest.raises(TurbineError) as malformed:
            await _result(client.turbine.job_settlement(_JOB_ID))
        assert malformed.value.code == "invalid_json_response"
        server.state.native_raw_body = None
        server.state.native_status = 503
        server.state.native_body = {
            "error": {
                "code": "settlement_unavailable",
                "message": "Unavailable for Bearer owner-key",
                "details": {"authorization": "owner-key"},
            }
        }
        server.state.native_response_headers = {
            "Retry-After": "2",
            "X-Comfy-Request-Id": "request1",
        }
        with pytest.raises(TurbineError) as temporary:
            await _result(client.turbine.job_settlement(_JOB_ID))
        assert temporary.value.code == "settlement_unavailable"
        assert temporary.value.http_status == 503
        assert temporary.value.request_id == "request1" and temporary.value.retry_after == 2
        assert "owner-key" not in str(temporary.value)
        assert temporary.value.details == {"authorization": "[redacted]"}
    finally:
        await _close(anonymous)
        await _close(client)
    assert server.state.http_requests == [("GET", f"/api/v2/jobs/{_JOB_ID}/settlement")] * 5


async def test_settlement_does_not_follow_redirects(server, client_type) -> None:
    server.state.job_settlement = _settlement()
    server.state.native_status = 302
    server.state.native_response_headers = {"Location": "/api/object_info"}
    borrowed_type = httpx2.AsyncClient if client_type is AsyncTurbine else httpx2.Client
    borrowed = borrowed_type(follow_redirects=True)
    client = client_type(api_key="owner-key", client=borrowed)
    try:
        with pytest.raises(TurbineError) as redirect:
            await _result(client.turbine.job_settlement(_JOB_ID))
        assert redirect.value.http_status == 302
    finally:
        await _close(client)
        if isinstance(borrowed, httpx2.AsyncClient):
            await borrowed.aclose()
        else:
            borrowed.close()
    assert server.state.http_requests == [("GET", f"/api/v2/jobs/{_JOB_ID}/settlement")]


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        pytest.param(("total",), _OMIT, id="missing-total"),
        pytest.param(("nodes",), _OMIT, id="missing-nodes"),
        pytest.param(("nodes",), [], id="empty-final"),
        pytest.param(("status",), "pending", id="pending-with-charges"),
        pytest.param(("status",), "not_applicable", id="not-applicable-with-charges"),
        pytest.param(("status",), "succeeded", id="execution-status"),
        pytest.param(("total", "amount"), "0", id="mismatched-total"),
        pytest.param(("total", "amount"), 9007199254740993, id="numeric-amount"),
        pytest.param(("nodes", 0, "charge", "unit"), "OTHER", id="wrong-unit"),
        pytest.param(("nodes", 0, "charge", "contract"), "other.token", id="wrong-contract"),
        pytest.param(("nodes", 0, "charge", "precision"), 2, id="wrong-precision"),
        pytest.param(("nodes", 0, "chain_id"), "4" * 64, id="wrong-chain"),
        pytest.param(("nodes", 1, "node_id"), "1", id="duplicate-node"),
        pytest.param(("nodes", 0, "version"), _OMIT, id="missing-tariff-version"),
        pytest.param(("nodes", 0, "model_id"), _OMIT, id="missing-model-id"),
        pytest.param(("nodes", 0, "outcome"), "released", id="released-with-charge"),
        pytest.param(("nodes", 1, "outcome"), "settled", id="settled-with-zero-charge"),
        pytest.param(("nodes", 0, "outcome"), "completed", id="execution-outcome"),
    ],
)
def test_settlement_rejects_invalid_evidence(path, replacement) -> None:
    reply = _settlement()
    parent = reply
    for key in path[:-1]:
        parent = parent[key]
    if replacement is _OMIT:
        del parent[path[-1]]
    else:
        parent[path[-1]] = replacement

    with pytest.raises(ValidationError):
        JobSettlement.model_validate(reply)


async def test_paid_partial_estimate_and_submit_preserve_the_same_selection(
    server, client_type
) -> None:
    server.state.native_estimate = _paid_estimate("7", "1")
    graph = {**_PAID_GRAPH, "2": {"class_type": "Other", "inputs": {"value": None}}}
    expected = {"prompt": graph, "partial_execution_targets": ["1"]}
    assert EstimateRequest.model_validate(expected).model_dump(mode="json") == expected
    assert EstimateRequest.model_validate({"prompt": graph}).model_dump(mode="json") == {
        "prompt": graph
    }
    client = client_type()
    try:
        estimate = await _result(
            client.turbine.estimate_prompt(graph, partial_execution_targets=["1"])
        )
        assert estimate.pricing is not None
        await _result(
            client.submit(
                client.workflows.from_json(graph),
                pricing=estimate.pricing,
                idempotency_key=_SUBMISSION_KEY,
                partial_execution_targets=["1"],
            )
        )
    finally:
        await _close(client)
    assert server.state.native_prompts == [expected]
    assert server.state.native_headers[0]["X-Turbine-Pricing"] == "1"
    assert server.state.last_jobs_body["workflow"] == graph
    assert server.state.last_jobs_body["partial_execution_targets"] == ["1"]
    assert server.state.last_jobs_body["x-turbine"]["pricing"] == estimate.pricing.model_dump()


@pytest.mark.parametrize("targets", [[], "1", [""], [1]])
async def test_invalid_partial_targets_are_rejected_before_http(
    server, client_type, targets
) -> None:
    client = client_type()
    try:
        with pytest.raises(ValidationError):
            await _result(
                client.turbine.estimate_prompt(_PAID_GRAPH, partial_execution_targets=targets)
            )
        with pytest.raises(ValueError, match="partial_execution_targets"):
            await _result(
                client.submit(
                    client.workflows.from_json(_PAID_GRAPH), partial_execution_targets=targets
                )
            )
    finally:
        await _close(client)
    assert server.state.http_requests == []


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
    assert [h.get("X-Turbine-Pricing") for h in server.state.native_headers] == [
        None,
        None,
        "1",
        None,
    ]
    assert all("/api/v2/" not in path for _, path in server.state.http_requests)
    assert server.state.upload_count == server.state.submit_count == 0


def test_paid_estimate_exposes_pricing_in_nested_response_schema() -> None:
    class Envelope(BaseModel):
        estimate: EstimateReply

    body = {"estimate": _paid_estimate("7", "1")}
    assert Envelope.model_validate(body).model_dump(mode="json") == body
    definitions = Envelope.model_json_schema(mode="serialization")["$defs"]
    estimate_schema = definitions["EstimateReply"]
    assert set(estimate_schema["required"]) == {"total", "nodes", "breakdown_complete"}
    assert set(estimate_schema["properties"]) == {
        "total",
        "nodes",
        "breakdown_complete",
        "x-turbine",
    }
    assert "PricingAuthorization" in definitions


async def test_paid_submit_preserves_the_exact_terms_and_rejects_stale_quote(
    server, client_type
) -> None:
    before = _paid_estimate("7", "1")
    after = _paid_estimate("11", "2")
    server.state.native_estimate = before
    client = client_type()
    try:
        workflow = client.workflows.from_json(_PAID_GRAPH)
        stale = await _result(client.turbine.estimate_prompt(workflow.json))
        assert isinstance(stale.pricing, PricingAuthorization)
        assert stale.model_dump(mode="json") == before
        assert EstimateReply.model_validate(stale) == stale

        server.state.native_estimate = after
        server.state.job_error = (409, "payment_terms_mismatch")
        with pytest.raises(TurbineError) as caught:
            await _result(
                client.submit(workflow, pricing=stale.pricing, idempotency_key=_SUBMISSION_KEY)
            )
        assert caught.value.code == "payment_terms_mismatch"
        assert caught.value.http_status == 409
        assert caught.value.idempotency_key == _SUBMISSION_KEY
        assert server.state.last_jobs_body == {
            "workflow": _PAID_GRAPH,
            "metadata": {"backend_job_id": _SUBMISSION_KEY},
            "x-turbine": before["x-turbine"],
        }
        assert server.state.idempotency == {}

        fresh = await _result(client.turbine.estimate_prompt(workflow.json))
        assert fresh.pricing is not None
        assert fresh.pricing.maximum.amount == "11"
        server.state.job_error = None
        labels = {"client": "example"}
        job = await _result(
            client.submit(
                workflow, pricing=fresh.pricing, idempotency_key=_SUBMISSION_KEY, metadata=labels
            )
        )
        assert labels == {"client": "example"}
        assert job.metadata == {"client": "example", "backend_job_id": _SUBMISSION_KEY}
        assert server.state.last_jobs_body == {
            "workflow": _PAID_GRAPH,
            "metadata": job.metadata,
            "x-turbine": after["x-turbine"],
        }
    finally:
        await _close(client)
    assert server.state.jobs_idempotency_keys == [_SUBMISSION_KEY, _SUBMISSION_KEY]
    assert server.state.idempotency == {_SUBMISSION_KEY: job.id}


async def test_paid_submit_validates_the_recoverable_key_before_post(server, client_type) -> None:
    estimate = EstimateReply.model_validate(_paid_estimate("7", "1"))
    client = client_type()
    try:
        workflow = client.workflows.from_json(_PAID_GRAPH)
        with pytest.raises(ValueError, match="explicit UUID"):
            await _result(client.submit(workflow, pricing=estimate.pricing))
        with pytest.raises(ValueError, match="canonical UUID"):
            await _result(
                client.submit(workflow, pricing=estimate.pricing, idempotency_key="printable")
            )
        with pytest.raises(ValueError, match="backend_job_id"):
            await _result(
                client.submit(
                    workflow,
                    pricing=estimate.pricing,
                    idempotency_key=_SUBMISSION_KEY,
                    metadata={"backend_job_id": "another"},
                )
            )
    finally:
        await _close(client)
    assert server.state.http_requests == []


async def test_lost_paid_201_recovers_by_label_and_rejects_changed_request(
    server, client_type
) -> None:
    estimate = EstimateReply.model_validate(_paid_estimate("7", "1"))
    server.state.drop_job_creation_response = True
    client = client_type()
    try:
        workflow = client.workflows.from_json(_PAID_GRAPH)
        with pytest.raises(httpx2.RemoteProtocolError) as lost:
            await _result(
                client.submit(workflow, pricing=estimate.pricing, idempotency_key=_SUBMISSION_KEY)
            )
        key = lost.value.idempotency_key
        assert key == _SUBMISSION_KEY
        job_id = server.state.idempotency[key]
        labels = {"backend_job_id": key}
        server.state.job_metadata = labels
        server.state.job_list_body = {
            "jobs": [
                {
                    "id": job_id,
                    "status": "queued",
                    "create_time": "2026-07-10T18:20:00Z",
                    "metadata": labels,
                }
            ]
        }
        if isinstance(client, AsyncTurbine):
            (summary,) = [item async for item in client.list_jobs(metadata=labels)]
        else:
            (summary,) = list(client.list_jobs(metadata=labels))
        job = await _result(client.jobs.get(summary.id))
        assert job.id == job_id
        assert job.metadata == labels
        assert server.state.job_list_queries == [{"metadata[backend_job_id]": [key]}]

        workflow.set_input("1", "text", "changed")
        with pytest.raises(IdempotencyKeyReuse):
            await _result(client.submit(workflow, pricing=estimate.pricing, idempotency_key=key))
        assert server.state.idempotency == {key: job_id}
    finally:
        await _close(client)


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
                    "package_hash": "b" * 64,
                    "operation_id": "text.complete",
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
    assert result.pricing is None
    assert server.state.native_headers[0]["X-Turbine-Pricing"] == "1"
    assert server.state.native_prompts == [{"prompt": raw}]
    assert server.state.http_requests == [("POST", "/api/prompt/estimate")]
    assert server.state.upload_count == server.state.submit_count == 0


async def test_paid_estimate_and_submit_use_the_same_v2_asset(server, client_type) -> None:
    server.state.native_estimate = _paid_estimate("7", "1")
    client = client_type()
    try:
        asset = client.assets.from_bytes(b"prepared", filename="input.png")
        reference = await _result(asset.as_reference())
        assert reference["__type"] == "core/ASSET"
        prepared = {"1": {"class_type": "Example", "inputs": {"image": reference}}}
        workflow = client.workflows.from_json(prepared)
        requests_before_estimate = len(server.state.http_requests)
        estimate = await _result(client.turbine.estimate_prompt(workflow.json))
        assert server.state.http_requests[requests_before_estimate:] == [
            ("POST", "/api/prompt/estimate")
        ]
        assert server.state.native_prompts == [{"prompt": prepared}]
        assert estimate.pricing is not None
        await _result(
            client.submit(workflow, pricing=estimate.pricing, idempotency_key=_SUBMISSION_KEY)
        )
        assert server.state.last_jobs_body["workflow"] == prepared
        assert server.state.upload_count == 1
    finally:
        await _close(client)


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
        (
            b'{"total":null,"nodes":[{"node_id":"1","model_id":null,'
            b'"price":{"amount":"125","precision":4,"unit":"USD"}}],'
            b'"breakdown_complete":false}',
            "invalid_schema_response",
        ),
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
        ("GET", "/api/v2/jobs/{id}/settlement"),
        ("GET", "/api/broker/v1/catalog"),
        ("GET", "/api/broker/v1/catalog/packages"),
        ("GET", "/api/broker/v1/catalog/packages/{package_hash}"),
    )
    for transport in (TurbineLow, AsyncTurbineLow):
        for name in (
            "get_node_catalog",
            "get_node_definition",
            "post_prompt_estimate",
            "get_job_settlement",
            "get_catalog_snapshot",
            "list_catalog_packages",
            "get_catalog_package",
        ):
            assert callable(getattr(transport, name))
