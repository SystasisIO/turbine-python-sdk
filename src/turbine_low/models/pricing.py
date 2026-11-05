"""Shared preview pricing schemas; no billing or execution promises."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    unit: str

    @field_validator("amount")
    @classmethod
    def _ascii_digits(cls, value: str) -> str:
        if not value or any(character < "0" or character > "9" for character in value):
            raise ValueError("amount must be a nonnegative integer encoded as ASCII digits")
        return value


class NodeEstimate(_PricingModel):
    """The known preview price of one node, with its optional model identity."""

    node_id: str
    model_id: str | None
    price: Money


class EstimateRequest(_PricingModel):
    """The complete estimate request: one prepared prompt graph."""

    prompt: PromptGraph


class EstimateReply(_PricingModel):
    """A price preview that can contain known nodes while the total is unknown.

    ``breakdown_complete`` describes the supplied breakdown. It neither admits
    an execution nor makes the preview an irrevocable final charge.
    """

    total: Money | None
    nodes: list[NodeEstimate]
    breakdown_complete: bool


__all__ = ["EstimateReply", "EstimateRequest", "Money", "NodeEstimate"]
