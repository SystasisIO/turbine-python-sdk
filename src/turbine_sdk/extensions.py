"""The native Turbine catalog and prepared-graph price-preview namespace."""

from __future__ import annotations

from typing import Any

from turbine_low.models.catalog import NodeCatalog
from turbine_low.models.graph import PromptGraph
from turbine_low.models.pricing import EstimateReply
from turbine_low.transport import AsyncTurbineLow, TurbineLow

from .exceptions import translating


class Nodes:
    """Native node definitions on this client's configured deployment."""

    def __init__(self, low: TurbineLow) -> None:
        self._low = low

    def list(self) -> NodeCatalog:
        """Return the server's complete class-type-to-definition mapping."""
        with translating():
            return self._low.get_node_catalog()

    def get(self, class_type: str) -> NodeCatalog:
        """Return the mapping supplied by the single-node native route."""
        with translating():
            return self._low.get_node_definition(class_type)


class AsyncNodes:
    """Async counterpart of :class:`Nodes`."""

    def __init__(self, low: AsyncTurbineLow) -> None:
        self._low = low

    async def list(self) -> NodeCatalog:
        """Return the server's complete class-type-to-definition mapping."""
        with translating():
            return await self._low.get_node_catalog()

    async def get(self, class_type: str) -> NodeCatalog:
        """Return the mapping supplied by the single-node native route."""
        with translating():
            return await self._low.get_node_definition(class_type)


class TurbineExtensions:
    """Native extensions sharing the client's transport, origin and credential."""

    def __init__(self, low: TurbineLow) -> None:
        self._low = low
        self.nodes = Nodes(low)

    def estimate_prompt(self, prompt: PromptGraph | dict[str, Any]) -> EstimateReply:
        """Preview a prepared JSON graph without uploading or running anything.

        Asset handles and other non-JSON values are rejected before HTTP. For
        a comparable preview and submission, use the same prepared graph for
        both; native file-reference and v2 asset equivalence is server-owned.
        """
        with translating():
            return self._low.post_prompt_estimate(prompt)


class AsyncTurbineExtensions:
    """Async counterpart of :class:`TurbineExtensions`."""

    def __init__(self, low: AsyncTurbineLow) -> None:
        self._low = low
        self.nodes = AsyncNodes(low)

    async def estimate_prompt(self, prompt: PromptGraph | dict[str, Any]) -> EstimateReply:
        """Preview a prepared JSON graph without uploading or running anything."""
        with translating():
            return await self._low.post_prompt_estimate(prompt)


__all__ = ["AsyncNodes", "AsyncTurbineExtensions", "Nodes", "TurbineExtensions"]
