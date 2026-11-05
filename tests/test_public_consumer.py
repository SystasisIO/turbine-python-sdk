"""An installed-package consumer journey using only the public SDK surface.

The same test and stdlib stub can run outside the checkout against a wheel.
It demonstrates graph identity, not live native-reference/v2-asset equivalence.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from turbine_sdk import AsyncTurbine, StatusChange, Turbine
from turbine_sdk.schemas import EstimateReply, NodeCatalog, PromptGraph

_FIXTURES = Path(__file__).parent / "fixtures" / "native"


class PreviewEnvelope(BaseModel):
    catalog: NodeCatalog
    prompt: PromptGraph
    estimate: EstimateReply


async def _result(value):
    return await value if inspect.isawaitable(value) else value


@pytest.mark.parametrize("client_type", [Turbine, AsyncTurbine], ids=["sync", "async"])
async def test_public_nodes_estimate_submit_events_wait_and_outputs(server, client_type) -> None:
    server.state.native_catalog = json.loads((_FIXTURES / "catalog.json").read_text())
    server.state.native_estimate = json.loads((_FIXTURES / "estimate-partial.json").read_text())
    raw_graph = json.loads((_FIXTURES / "graph.json").read_text())
    client = client_type(base_url=server.base_url, api_key="consumer-key")
    try:
        catalog = await _result(client.turbine.nodes.list())
        assert raw_graph["1"]["class_type"] in catalog.root
        graph = PromptGraph.model_validate(raw_graph)
        estimate = await _result(client.turbine.estimate_prompt(graph))
        envelope = PreviewEnvelope(catalog=catalog, prompt=graph, estimate=estimate)
        assert PreviewEnvelope.model_validate_json(envelope.model_dump_json()) == envelope
        assert envelope.model_dump()["catalog"] == server.state.native_catalog
        assert envelope.estimate.total is None
        assert envelope.estimate.nodes[0].price.amount == "125"

        # An estimate did not perform the execution or upload on our behalf.
        assert server.state.http_requests == [
            ("GET", "/api/object_info"),
            ("POST", "/api/prompt/estimate"),
        ]
        workflow = client.workflows.from_json(graph.model_dump(mode="json"))
        job = await _result(client.submit(workflow))
        if isinstance(client, AsyncTurbine):
            events = [event async for event in job.events()]
        else:
            events = list(job.events())
        assert isinstance(events[-1], StatusChange)
        assert events[-1].status == "succeeded"
        finished = await _result(job.wait(timeout=2))
        assert finished.status == "succeeded"
        assert await _result(finished.outputs[0].to_bytes()) == server.state.content_bytes
    finally:
        if isinstance(client, AsyncTurbine):
            await client.aclose()
        else:
            client.close()
    assert server.state.native_prompts == [{"prompt": raw_graph}]
    assert server.state.last_jobs_body["workflow"] == raw_graph
    assert server.state.submit_count == 1
    assert server.state.upload_count == 0
    assert server.state.events_connect_count == 1
