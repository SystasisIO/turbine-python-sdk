"""Shared Turbine pricing preview and submission authorization schemas."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from ._scalars import Identifier, Sha256Digest
from .graph import PromptGraph


class _PricingModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        protected_namespaces=("model_validate", "model_dump"),
    )


class Money(_PricingModel):
    """An exact nonnegative integer amount with a decimal precision and unit."""

    amount: Annotated[str, Field(pattern=r"^[0-9]+$")]
    precision: Annotated[int, Field(ge=0)]
    unit: Annotated[str, Field(min_length=1)]

    @field_validator("amount")
    @classmethod
    def _ascii_digits(cls, value: str) -> str:
        if not value or any(character < "0" or character > "9" for character in value):
            raise ValueError("amount must be a nonnegative integer encoded as ASCII digits")
        return value


class NodeEstimate(_PricingModel):
    """The known price of a node with its resolved package, operation, and model."""

    node_id: Annotated[str, Field(min_length=1)]
    package_hash: Sha256Digest
    operation_id: Identifier
    model_id: Annotated[str, Field(min_length=1)] | None
    price: Money


class PricingAsset(Money):
    """A spending limit denominated in a specific chain token."""

    amount: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]
    precision: Annotated[int, Field(ge=0, le=255)]
    unit: Annotated[str, Field(pattern=r"^[A-Z]{1,7}$")]
    contract: Annotated[str, Field(pattern=r"^([.1-5a-z]{0,11}[1-5a-z]|[.1-5a-z]{12}[1-5a-j])$")]

    @field_validator("amount")
    @classmethod
    def _asset_range(cls, value: str) -> str:
        if int(value) > (1 << 62) - 1:
            raise ValueError("amount exceeds the chain asset range")
        return value


class NodePricingAuthorization(_PricingModel):
    """The exact release, binding and tariff with a limit for one node."""

    node_id: Identifier
    package_hash: Sha256Digest
    operation_id: Identifier
    binding_id: Identifier
    model_id: Identifier | None
    chain_id: Sha256Digest
    service: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    terms_hash: Sha256Digest
    binding_digest: Sha256Digest
    maximum: PricingAsset

    @field_validator("service", "version")
    @classmethod
    def _uint64_range(cls, value: str) -> str:
        if int(value) > (1 << 64) - 1:
            raise ValueError("tariff identifier exceeds the unsigned 64-bit range")
        return value


class PricingAuthorization(_PricingModel):
    """Client-authorized maximum for the graph and exact terms for each node."""

    maximum: PricingAsset
    nodes: Annotated[list[NodePricingAuthorization], Field(min_length=1, max_length=4096)]


class _EstimateExtension(_PricingModel):
    pricing: PricingAuthorization


class EstimateRequest(_PricingModel):
    """The complete estimate request: one prepared prompt graph."""

    prompt: PromptGraph


class EstimateReply(_PricingModel):
    """A price preview that can contain known nodes while the total is unknown.

    ``pricing`` contains the proposed terms when the estimate can price every
    selected node within the chain asset range. Passing them to submit asks
    the server to validate the terms again before accepting paid execution.
    """

    model_config = ConfigDict(populate_by_name=True)

    total: Money | None
    nodes: list[NodeEstimate]
    breakdown_complete: bool
    extension: _EstimateExtension | None = Field(default=None, alias="x-turbine")

    @property
    def pricing(self) -> PricingAuthorization | None:
        return self.extension.pricing if self.extension is not None else None

    # Leave the return type inferred so Pydantic derives the response schema from the fields.
    @model_serializer(mode="wrap")
    def _serialize_extension(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo):
        data: dict[str, Any] = handler(self)
        extension = data.pop("x-turbine" if info.by_alias else "extension", None)
        if extension is not None:
            data["x-turbine"] = extension
        return data


__all__ = [
    "EstimateReply",
    "EstimateRequest",
    "Money",
    "NodeEstimate",
    "NodePricingAuthorization",
    "PricingAsset",
    "PricingAuthorization",
]
