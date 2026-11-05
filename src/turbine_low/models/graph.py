"""Prepared, JSON-only prompt graphs shared by the SDK's native routes."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, RootModel, model_validator


def _require_json(value: Any, active: set[int] | None = None) -> Any:
    """Reject Python objects before Pydantic can coerce or serialize them.

    In particular an asset handle, a path, bytes, or another model is not a
    prepared file reference. No input values are included in the error.
    """
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("prompt and catalog values must be finite JSON values")
        return value
    if type(value) not in (dict, list):
        raise ValueError("prompt and catalog values must contain only JSON values")
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise ValueError("prompt and catalog values must not contain cycles")
    active.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                _require_json(item, active)
        else:
            for item in value:
                _require_json(item, active)
    finally:
        active.remove(identity)
    return value


def _model_values(value: BaseModel) -> dict[str, Any]:
    """Read raw values, retaining omitted fields and refusing hidden coercion.

    ``model_dump`` would serialize a dataclass placed in a mutated model. Raw
    values let the JSON check catch it before an estimate can send anything.
    """
    fields = type(value).model_fields
    result = {name: getattr(value, name) for name in value.model_fields_set if name in fields}
    result.update(value.model_extra or {})
    return result


class PromptNode(BaseModel):
    """One native prompt node; links, ``_meta`` and extra JSON keys are retained."""

    model_config = ConfigDict(extra="allow", strict=True, revalidate_instances="always")

    class_type: str
    inputs: dict[str, JsonValue]

    @model_validator(mode="before")
    @classmethod
    def _json_values(cls, value: Any) -> Any:
        if isinstance(value, cls):
            value = _model_values(value)
        return _require_json(value)


class PromptGraph(RootModel[dict[str, PromptNode]]):
    """An API-format graph already containing JSON references instead of handles.

    Node IDs and input links are preserved as sent. This model does not resolve
    assets, infer graph dependencies, or convert native references to v2 assets.
    """

    model_config = ConfigDict(strict=True, revalidate_instances="always")

    @model_validator(mode="before")
    @classmethod
    def _prepared_graph(cls, value: Any) -> Any:
        if isinstance(value, cls):
            value = value.root
        if isinstance(value, dict):
            value = {
                key: _model_values(node) if isinstance(node, PromptNode) else node
                for key, node in value.items()
            }
        return _require_json(value)


__all__ = ["PromptGraph", "PromptNode"]
