"""The native Turbine catalog and prepared-graph price-preview namespace."""

from __future__ import annotations

from typing import Any

from turbine_low.models.catalog import NodeCatalog
from turbine_low.models.graph import PromptGraph
from turbine_low.models.packages import CATALOG_DEFAULT_LIMIT, CatalogSnapshot, Package, PackagePage
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


class Packages:
    """Service package catalog on this client's configured deployment."""

    def __init__(self, low: TurbineLow) -> None:
        self._low = low

    def snapshot(self) -> CatalogSnapshot:
        """Read a catalog revision for the current service owner."""
        with translating():
            return self._low.get_catalog_snapshot()

    def list(
        self,
        *,
        catalog_revision: str,
        cursor: str | None = None,
        limit: int = CATALOG_DEFAULT_LIMIT,
    ) -> PackagePage:
        """Read one page from the specified revision."""
        with translating():
            return self._low.list_catalog_packages(
                catalog_revision=catalog_revision, cursor=cursor, limit=limit
            )

    def get(self, package_hash: str, *, catalog_revision: str) -> Package:
        """Read the exact package release from the specified revision."""
        with translating():
            return self._low.get_catalog_package(package_hash, catalog_revision=catalog_revision)


class AsyncPackages:
    """Async counterpart of :class:`Packages`."""

    def __init__(self, low: AsyncTurbineLow) -> None:
        self._low = low

    async def snapshot(self) -> CatalogSnapshot:
        """Read a catalog revision for the current service owner."""
        with translating():
            return await self._low.get_catalog_snapshot()

    async def list(
        self,
        *,
        catalog_revision: str,
        cursor: str | None = None,
        limit: int = CATALOG_DEFAULT_LIMIT,
    ) -> PackagePage:
        """Read one page from the specified revision."""
        with translating():
            return await self._low.list_catalog_packages(
                catalog_revision=catalog_revision, cursor=cursor, limit=limit
            )

    async def get(self, package_hash: str, *, catalog_revision: str) -> Package:
        """Read the exact package release from the specified revision."""
        with translating():
            return await self._low.get_catalog_package(
                package_hash, catalog_revision=catalog_revision
            )


class TurbineExtensions:
    """Native extensions sharing the client's transport, origin and credential."""

    def __init__(self, low: TurbineLow) -> None:
        self._low = low
        self.nodes = Nodes(low)
        self.packages = Packages(low)

    def estimate_prompt(self, prompt: PromptGraph | dict[str, Any]) -> EstimateReply:
        """Preview a prepared JSON graph without uploading or running anything.

        For v2 asset inputs, prepare the graph with an uploaded asset's
        ``as_reference()`` and pass that same graph to submit. Asset handles
        and other non-JSON values are rejected before HTTP.
        """
        with translating():
            return self._low.post_prompt_estimate(prompt)


class AsyncTurbineExtensions:
    """Async counterpart of :class:`TurbineExtensions`."""

    def __init__(self, low: AsyncTurbineLow) -> None:
        self._low = low
        self.nodes = AsyncNodes(low)
        self.packages = AsyncPackages(low)

    async def estimate_prompt(self, prompt: PromptGraph | dict[str, Any]) -> EstimateReply:
        """Preview a prepared JSON graph without uploading or running anything."""
        with translating():
            return await self._low.post_prompt_estimate(prompt)


__all__ = [
    "AsyncNodes",
    "AsyncPackages",
    "AsyncTurbineExtensions",
    "Nodes",
    "Packages",
    "TurbineExtensions",
]
