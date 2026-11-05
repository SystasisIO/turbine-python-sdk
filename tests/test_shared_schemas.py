"""Shared native schemas preserve wire data and compose as Pydantic values."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from turbine_sdk import schemas
from turbine_sdk.schemas import (
    EstimateReply,
    EstimateRequest,
    Money,
    NodeCatalog,
    NodeDefinition,
    NodeEstimate,
    NodeInputs,
    PromptGraph,
    PromptNode,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


@pytest.mark.parametrize("amount", ["0", "0001", "184467440737095516150000000000000000001"])
def test_money_retains_an_exact_ascii_integer_string(amount: str) -> None:
    money = Money(amount=amount, precision=42, unit="custom-unit")
    assert json.loads(money.model_dump_json()) == {
        "amount": amount,
        "precision": 42,
        "unit": "custom-unit",
    }


@pytest.mark.parametrize(
    "amount", [0, 1.0, True, None, "", "-1", "+1", "1.0", "1e3", " 1", "1\n", "١", "１２"]
)
def test_money_refuses_non_ascii_or_non_integer_amounts(amount: object) -> None:
    with pytest.raises(ValidationError):
        Money(amount=amount, precision=2, unit="USD")


@pytest.mark.parametrize("precision", [True, False, -1, "2", 2.0, None])
def test_money_precision_is_a_nonnegative_integer_not_boolean(precision: object) -> None:
    with pytest.raises(ValidationError):
        Money(amount="1", precision=precision, unit="USD")


@pytest.mark.parametrize("validate_json", [False, True])
@pytest.mark.parametrize(
    "model,body,field",
    [
        (Money, {"amount": "1", "precision": 0, "unit": ""}, "unit"),
        (
            NodeEstimate,
            {
                "node_id": "",
                "model_id": None,
                "price": {"amount": "1", "precision": 0, "unit": "x"},
            },
            "node_id",
        ),
        (
            NodeEstimate,
            {
                "node_id": "1",
                "model_id": "",
                "price": {"amount": "1", "precision": 0, "unit": "x"},
            },
            "model_id",
        ),
    ],
)
def test_pricing_strings_cannot_be_empty(model, body, field, validate_json: bool) -> None:
    with pytest.raises(ValidationError) as exc:
        if validate_json:
            model.model_validate_json(json.dumps(body))
        else:
            model.model_validate(body)
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        ((field,), "string_too_short")
    ]


@pytest.mark.parametrize("value", ["x", " ", " custom/модель "])
@pytest.mark.parametrize("null_model", [False, True])
def test_pricing_strings_roundtrip_without_normalization(value: str, null_model: bool) -> None:
    body = {
        "node_id": value,
        "model_id": None if null_model else value,
        "price": {"amount": "1", "precision": 0, "unit": value},
    }
    estimate = NodeEstimate.model_validate(body)
    assert estimate.model_dump(mode="json") == body
    assert json.loads(estimate.model_dump_json()) == body
    assert NodeEstimate.model_validate_json(estimate.model_dump_json()) == estimate


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_pricing_json_schemas_require_nonempty_strings_and_allow_null_model(mode: str) -> None:
    for model, field in [(Money, "unit"), (NodeEstimate, "node_id")]:
        schema = model.model_json_schema(mode=mode)
        property_schema = schema["properties"][field]
        assert property_schema["type"] == "string"
        assert property_schema["minLength"] == 1
        assert field in schema["required"]
        assert "default" not in property_schema
    node_schema = NodeEstimate.model_json_schema(mode=mode)
    model_schema = node_schema["properties"]["model_id"]
    assert model_schema["anyOf"] == [{"type": "string", "minLength": 1}, {"type": "null"}]
    assert "model_id" in node_schema["required"]
    assert "default" not in model_schema


@pytest.mark.parametrize(
    "model,body,required",
    [
        (Money, {"amount": "1", "precision": 2, "unit": "USD"}, {"amount", "precision", "unit"}),
        (
            NodeEstimate,
            {
                "node_id": "1",
                "model_id": None,
                "price": {"amount": "1", "precision": 0, "unit": "x"},
            },
            {"node_id", "model_id", "price"},
        ),
        (EstimateRequest, {"prompt": {}}, {"prompt"}),
        (
            EstimateReply,
            {"total": None, "nodes": [], "breakdown_complete": False},
            {"total", "nodes", "breakdown_complete"},
        ),
    ],
)
def test_all_pricing_fields_are_required_even_when_nullable(model, body, required) -> None:
    assert set(model.model_json_schema()["required"]) == required
    for field in required:
        missing = {key: value for key, value in body.items() if key != field}
        with pytest.raises(ValidationError):
            model.model_validate(missing)
    with pytest.raises(ValidationError):
        model.model_validate({**body, "quote_id": "invented"})


def test_partial_estimate_preserves_unknown_total_and_known_nodes() -> None:
    body = _fixture("estimate-partial.json")
    estimate = EstimateReply.model_validate(body)
    assert estimate.total is None
    assert estimate.nodes[0].price.amount == "125"
    assert estimate.nodes[0].model_id is None
    assert estimate.model_dump(mode="json") == body
    with pytest.raises(ValidationError):
        EstimateReply.model_validate({**body, "breakdown_complete": 1})


def test_catalog_preserves_widget_descriptors_metadata_order_and_omissions() -> None:
    body = _fixture("catalog.json")
    catalog = NodeCatalog.model_validate(body)
    assert catalog.model_dump(mode="json") == body
    example = catalog.root["Example"]
    assert example.input.required["mode"][0] == ["fast", "quality", "balanced"]
    assert list(example.input.required) == ["text", "mode", "seed"]
    assert example.display_name is None and "display_name" in example.model_fields_set
    assert example.python_module is None and "python_module" not in example.model_fields_set
    assert example.input.optional is None and "optional" in example.input.model_fields_set
    assert catalog.root["Minimal"].model_dump() == {"output": []}
    assert NodeCatalog.model_validate_json(catalog.model_dump_json()).model_dump() == body


def test_typed_catalog_construction_does_not_mutate_the_supplied_dictionary() -> None:
    inputs = NodeInputs(required={"value": ["INT", {"default": 1}]})
    definition = {"input": inputs, "category": None}
    source = {"Example": definition}
    result = NodeCatalog.model_validate(source)
    assert source["Example"] is definition
    assert definition["input"] is inputs
    assert result.model_dump() == {
        "Example": {"input": {"required": {"value": ["INT", {"default": 1}]}}, "category": None}
    }
    typed = NodeCatalog({"Example": NodeDefinition(input=inputs)})
    assert typed.model_dump() == {"Example": {"input": inputs.model_dump()}}


def test_graph_retains_links_meta_and_unknown_json_fields() -> None:
    body = _fixture("graph.json")
    graph = PromptGraph.model_validate(body)
    assert graph.root["1"].inputs["image"] == ["10", 0]
    assert graph.root["1"].model_extra["_meta"] == body["1"]["_meta"]
    assert graph.model_dump(mode="json") == body
    assert PromptGraph.model_validate_json(graph.model_dump_json()).model_dump() == body
    assert PromptGraph({"1": PromptNode(class_type="Example", inputs={})}).model_dump() == {
        "1": {"class_type": "Example", "inputs": {}}
    }


@pytest.mark.parametrize(
    "value",
    [
        b"file",
        Path("file.png"),
        Decimal("1.5"),
        ("1", 0),
        {1, 2},
        float("nan"),
        float("inf"),
        object(),
    ],
)
def test_graph_accepts_only_prepared_json_values(value: object) -> None:
    with pytest.raises(ValidationError):
        PromptGraph.model_validate({"1": {"class_type": "Example", "inputs": {"value": value}}})


def test_graph_refuses_non_string_keys_and_cycles() -> None:
    with pytest.raises(ValidationError):
        PromptGraph.model_validate({1: {"class_type": "Example", "inputs": {}}})
    cyclic: dict = {}
    cyclic["loop"] = cyclic
    with pytest.raises(ValidationError):
        PromptGraph.model_validate({"1": {"class_type": "Example", "inputs": cyclic}})


def test_shared_models_compose_without_losing_native_data() -> None:
    class Envelope(BaseModel):
        catalog: NodeCatalog
        prompt: PromptGraph
        estimate: EstimateReply

    body = {
        "catalog": _fixture("catalog.json"),
        "prompt": _fixture("graph.json"),
        "estimate": _fixture("estimate-partial.json"),
    }
    envelope = Envelope.model_validate(body)
    assert envelope.model_dump(mode="json") == body
    assert Envelope.model_validate_json(envelope.model_dump_json()) == envelope
    assert {"Money", "NodeDefinition", "PromptGraph"} <= Envelope.model_json_schema()[
        "$defs"
    ].keys()


@pytest.mark.parametrize(
    "name,module",
    [
        ("NodeCatalog", "catalog"),
        ("NodeDefinition", "catalog"),
        ("NodeInputs", "catalog"),
        ("PromptGraph", "graph"),
        ("PromptNode", "graph"),
        ("Money", "pricing"),
        ("NodeEstimate", "pricing"),
        ("EstimateRequest", "pricing"),
        ("EstimateReply", "pricing"),
    ],
)
def test_public_schemas_reexport_the_exact_low_class_objects(name: str, module: str) -> None:
    assert name in schemas.__all__
    low = importlib.import_module(f"turbine_low.models.{module}")
    assert getattr(schemas, name) is getattr(low, name)


def test_schema_import_needs_no_client_configuration_or_network() -> None:
    code = """
import os
import socket
import sys
os.environ['TURBINE_BASE_URL'] = 'malformed URL that must not be read at import'
os.environ['TURBINE_ROUTER_BASE_URL'] = 'also malformed'
def no_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('schema import attempted a network connection')
sys.addaudithook(no_network)
try:
    socket.getaddrinfo('example.invalid', 80)
except AssertionError:
    pass
else:
    raise AssertionError('the network guard did not intercept name resolution')
from turbine_sdk.schemas import EstimateReply, Money, NodeCatalog, PromptGraph
assert Money(amount='1', precision=0, unit='x').amount == '1'
assert EstimateReply(total=None, nodes=[], breakdown_complete=False).total is None
assert NodeCatalog.model_json_schema()
assert PromptGraph.model_json_schema()
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
