"""Spec coverage: every operationId in spec/openapi.yaml must have a transport
function on both the sync and async ``turbine_low`` transports.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import turbine_low
from turbine_low.transport import AsyncTurbineLow, TurbineLow

SPEC = Path(__file__).resolve().parent.parent / "spec" / "openapi.yaml"


def _spec_operation_ids() -> set[str]:
    doc = yaml.safe_load(SPEC.read_text())
    ids: set[str] = set()
    for _path, methods in doc["paths"].items():
        for method, op in methods.items():
            if method in ("get", "post", "put", "patch", "delete", "head"):
                # An operation tagged internal would be stripped before vendoring.
                if op.get("x-internal") is True or "internal" in op.get("tags", []):
                    continue
                ids.add(op["operationId"])
    return ids


def test_declared_operation_ids_match_spec() -> None:
    assert turbine_low.OPERATION_IDS == _spec_operation_ids()


@pytest.mark.parametrize("transport", [TurbineLow, AsyncTurbineLow])
def test_every_operation_has_a_transport_function(transport: type) -> None:
    for op_id, method_name in turbine_low.OPERATION_METHODS.items():
        assert op_id in turbine_low.OPERATION_IDS
        assert callable(getattr(transport, method_name, None)), (
            f"{transport.__name__} missing method {method_name} for {op_id}"
        )


def test_operation_methods_cover_every_operation() -> None:
    assert set(turbine_low.OPERATION_METHODS) == turbine_low.OPERATION_IDS
