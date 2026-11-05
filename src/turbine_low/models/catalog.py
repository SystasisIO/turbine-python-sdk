"""Dynamic native node definitions without a generated class per node type."""

from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    RootModel,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from .graph import _model_values, _require_json


class _CatalogObject(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, revalidate_instances="always")

    @model_serializer(mode="wrap")
    def _keep_omissions(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        # Optional native fields have no invented default on the wire. An
        # explicitly supplied null stays null, including inside other models.
        for name in type(self).model_fields:
            if name not in self.model_fields_set:
                data.pop(name, None)
        return data


class NodeInputs(_CatalogObject):
    """Input groups containing the server's unmodified widget descriptors.

    A descriptor can carry a type name, enum choices, options and future JSON
    shapes. Keeping its JSON value retains array order and nested metadata.
    """

    required: dict[str, JsonValue] | None = None
    optional: dict[str, JsonValue] | None = None
    hidden: dict[str, JsonValue] | None = None

    @model_validator(mode="before")
    @classmethod
    def _json_values(cls, value: Any) -> Any:
        return _require_json(_model_values(value) if isinstance(value, cls) else value)


class NodeDefinition(_CatalogObject):
    """A node definition with common fields and arbitrary additional metadata.

    Fields omitted by the server remain omitted when serialized. These models
    describe catalog data rather than validating every package's input schema.
    """

    input: NodeInputs | None = None
    input_order: dict[str, list[str]] | None = None
    output: list[str] | None = None
    output_is_list: list[bool] | None = None
    output_name: list[str] | None = None
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    python_module: str | None = None
    category: str | None = None
    output_node: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _json_values(cls, value: Any) -> Any:
        if isinstance(value, cls):
            value = _model_values(value)
        if isinstance(value, dict) and isinstance(value.get("input"), NodeInputs):
            value = {**value, "input": _model_values(value["input"])}
        return _require_json(value)


class NodeCatalog(RootModel[dict[str, NodeDefinition]]):
    """The class-type-to-definition mapping returned by either object-info route."""

    model_config = ConfigDict(strict=True, revalidate_instances="always")

    @model_validator(mode="before")
    @classmethod
    def _json_definitions(cls, value: Any) -> Any:
        if isinstance(value, cls):
            value = value.root
        if isinstance(value, dict):
            definitions = {}
            for key, item in value.items():
                definition = _model_values(item) if isinstance(item, NodeDefinition) else item
                if isinstance(definition, dict) and isinstance(definition.get("input"), NodeInputs):
                    definition = {**definition, "input": _model_values(definition["input"])}
                definitions[key] = definition
            value = definitions
        return _require_json(value)


__all__ = ["NodeCatalog", "NodeDefinition", "NodeInputs"]
