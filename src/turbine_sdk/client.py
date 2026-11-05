"""Sync and async TurbineOS clients over one shared protocol transport.

``base_url`` or ``TURBINE_BASE_URL`` selects the deployment. Router is enabled
only by an explicit ``router_base_url``. Omit ``api_key`` to read
``TURBINE_API_KEY``; pass ``None`` to omit the SDK bearer credential.
Injected HTTPX2 clients stay owned and configured by their caller.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import httpx2

from turbine_low.errors import ApiError
from turbine_low.transport import (
    AsyncTurbineLow,
    TurbineLow,
    _validate_base_url,
    metadata_filter_pairs,
)

from . import _core
from .assets import AssetFactory, AsyncAssetFactory
from .exceptions import TurbineError, WorkflowFormatUi, to_sdk_error, translating
from .extensions import AsyncTurbineExtensions, TurbineExtensions
from .jobs import AsyncJob, AsyncJobFactory, Job, JobFactory, JobSummary
from .models import AsyncModels, Models
from .retry import DEFAULT_RETRY, RetryPolicy
from .workflows import Workflow, WorkflowFactory

# How long to keep retrying a full queue before giving up (seconds).
_QUEUE_RETRY_BUDGET = 60.0
#: Environment variable read when no explicit deployment URL is supplied.
BASE_URL_ENV_VAR = "TURBINE_BASE_URL"
#: Environment variable read only when the ``api_key`` argument is omitted.
API_KEY_ENV_VAR = "TURBINE_API_KEY"


class _ApiKeyUnset:
    """Distinguish an omitted key from explicit ``None`` internally."""


_API_KEY_UNSET = _ApiKeyUnset()

_DEFAULT_RETRY_AFTER = 2
# The shortest wait before a 429 retry, so a `Retry-After: 0` (or a negative
# one) cannot drive a tight loop against the server.
_MIN_RETRY_DELAY = 1.0
_now = time.monotonic


def _retry_delay(exc: ApiError, deadline: float) -> float | None:
    """Return a bounded 429 retry delay, or ``None`` when the error should surface."""
    if exc.http_status != 429:
        return None
    if exc.retry_after is None:
        # The spec requires Retry-After on deployment_not_ready. Keep the
        # fallback only for legacy queue_full responses that omit it.
        if exc.code != "queue_full":
            return None
        raw_delay = _DEFAULT_RETRY_AFTER
    else:
        raw_delay = exc.retry_after
    remaining = deadline - _now()
    if remaining <= 0:
        return None
    return min(max(raw_delay, _MIN_RETRY_DELAY), remaining)


def _page_jobs(page: Any) -> list[Any]:
    """The ``jobs`` list of one :meth:`Turbine.list_jobs` page (empty when absent or null)."""
    if not isinstance(page, dict):
        raise TurbineError("job list response is not a JSON object", code="invalid_response")
    jobs = page.get("jobs")
    if jobs is None:
        return []
    if not isinstance(jobs, list):
        raise TurbineError(
            "job list response field 'jobs' is not an array", code="invalid_response"
        )
    return jobs


def _next_list_cursor(page: dict[str, Any], sent: set[str]) -> str | None:
    """The cursor for the :meth:`Turbine.list_jobs` page after ``page``; ``None`` ends the list.

    A missing, null or empty ``next_cursor`` is the last page; any other value
    that is not a string (``0``, ``false`` and ``[]`` included) raises. A
    cursor this iteration already sent would fetch a page it already read,
    again and again, so it raises instead of being followed.
    """
    cursor = page.get("next_cursor")
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str):
        raise TurbineError(
            "job list response field 'next_cursor' is not a string", code="invalid_response"
        )
    if cursor in sent:
        raise TurbineError(
            "job list returned a cursor it already returned; stopping to avoid a loop",
            code="invalid_response",
        )
    sent.add(cursor)
    return cursor


def _has_labels(summary: JobSummary, metadata: Mapping[str, str] | None) -> bool:
    """Whether ``summary`` carries every ``metadata`` filter pair (always, with none).

    Each pair is compared as the text the query string sends, as the server
    compares it, so ``{"run": 7}`` matches the label ``"7"`` and ``{1: "a"}``
    the key ``"1"``.
    """
    return all(summary.metadata.get(k) == v for k, v in metadata_filter_pairs(metadata))


def _resolve_base_url(explicit: str | None) -> str:
    if explicit is not None:
        return _validate_base_url(explicit, "base_url")
    raw = os.environ.get(BASE_URL_ENV_VAR)
    if raw is None:
        raise ValueError(f"pass base_url=... or set {BASE_URL_ENV_VAR}")
    return _validate_base_url(raw, BASE_URL_ENV_VAR)


def _resolve_api_key(explicit: str | None | _ApiKeyUnset) -> str | None:
    if isinstance(explicit, _ApiKeyUnset):
        return os.environ.get(API_KEY_ENV_VAR, "").strip() or None
    if explicit is None:
        return None
    if not isinstance(explicit, str) or not explicit.strip():
        raise ValueError("api_key must be a non-blank string or None")
    return explicit.strip()


def _guard_ui_format(workflow: Workflow) -> None:
    if _core.looks_like_ui_format(workflow.json):
        raise WorkflowFormatUi(
            "workflow is in UI-export format (nodes/links/last_node_id); submit "
            "the API-format graph instead",
            code="workflow_format_ui",
            http_status=422,
        )


class Turbine:
    """Synchronous TurbineOS client.

    Configure a deployment through ``base_url`` or ``TURBINE_BASE_URL``.
    ``client`` optionally supplies a caller-owned HTTPX2 pool; Router is
    disabled until ``router_base_url`` is explicitly supplied.
    ``retry`` governs Router calls; workflow submit keeps its existing 429 policy.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None | _ApiKeyUnset = _API_KEY_UNSET,
        router_base_url: str | None = None,
        client: httpx2.Client | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        base_url = _resolve_base_url(base_url)
        key = _resolve_api_key(api_key)
        self._low = TurbineLow(
            base_url,
            key,
            timeout=timeout,
            client_info=client_info,
            router_base_url=router_base_url,
            client=client,
        )
        self.assets = AssetFactory(self._low)
        self.workflows = WorkflowFactory()
        self.jobs = JobFactory(self._low)
        #: ``client.models`` — the model namespace, sharing this client's
        #: transport (credentials, base URL, connection pool, timeout) and its
        #: retry policy.
        self.models = Models(self._low, retry)
        self.turbine = TurbineExtensions(self._low)

    def close(self) -> None:
        """Close the HTTP pool when the SDK created it.

        A caller-supplied HTTPX2 client stays open. Prefer the context-manager
        form (``with Turbine(...) as client:``) for an SDK-owned pool.
        """
        self._low.close()

    def __enter__(self) -> Turbine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        """Target and *whether* a key is attached — never the key itself.

        Explicit rather than inherited: the default ``object`` repr happens not
        to leak, but a client is exactly the object that ends up in a traceback,
        a debugger frame, or a CI log, so what it renders is stated here on
        purpose instead of left to whoever edits this class next.
        """
        return (
            f"{type(self).__name__}(base_url={self._low.safe_base_url!r}, "
            f"authenticated={self._low.authenticated})"
        )

    def _materialize(self, workflow: Workflow) -> dict[str, Any]:
        """Commit every embedded asset handle and substitute ``core/ASSET`` refs."""
        handles = _core.find_asset_handles(workflow.json)
        refs: dict[int, dict[str, Any]] = {}
        for h in handles:
            h.commit()
            refs[id(h)] = h.as_reference()
        return _core.substitute_asset_handles(workflow.json, refs)

    def submit(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Job:
        """Submit a workflow. Retries any 429 that carries ``Retry-After``.

        Sends an auto-generated ``Idempotency-Key`` so the server rejects an
        accidental exact resend of *this* request (``422 idempotency_key_reuse``)
        instead of creating a duplicate job. Each call mints a fresh key, so
        calling ``submit()`` again is a distinct submission — to make a retry
        idempotent, pass an explicit ``idempotency_key`` and reuse it. Note a
        reused key is *rejected*, not replayed: on reuse, catch the error and
        poll/list for the job the first attempt already created.

        A caller-supplied ``idempotency_key`` is validated the way
        ``models.run`` validates its own — an empty, over-long, or
        non-printable-ASCII key raises ``ValueError`` here, before any request
        — so an explicit ``""`` never silently mints a fresh key.

        Every exception the ``POST /jobs`` attempt itself raises carries the
        key it was made under on ``.idempotency_key``: the mapped
        ``TurbineError``, a ``QueueFull`` once the retry budget is spent, and a
        transport failure that never reached a response (``httpx2.ConnectError``,
        a read timeout), which reads ``.request_id`` and ``.retry_after`` as
        ``None`` rather than raising. A failure raised before the request
        exists — a UI-format workflow, an asset that would not upload — carries
        none. Because a reused key is rejected rather than replayed, the key is
        not a replay handle. Whether the server ever saw it depends on the
        failure: a connect failure never delivered it and a ``QueueFull`` was
        refused on every attempt, so the key is unclaimed and resubmitting
        under it is fine; after an ambiguous failure (a read timeout, a
        connection dropped mid-request) poll or list for the job the first
        attempt may already have created rather than resubmitting under it.

        ``api_key`` authenticates partner (API) nodes embedded in the workflow
        (e.g. Gemini) — unrelated to idempotency and unrelated to the bearer
        token this client was constructed with. It is never persisted or
        logged by the SDK, and is sent as ``extra_data.api_key_comfy_org``
        only when supplied; omitted from the request entirely otherwise.

        ``metadata`` is a map of string labels stored on the job (for example
        ``{"client": "acme"}``), read back from :attr:`Job.metadata` and
        matched by :meth:`list_jobs`. It is sent as given: the server owns the
        limits on keys, values and count, and a map it refuses raises
        :class:`~turbine_sdk.exceptions.TurbineError` with ``code ==
        "metadata_invalid"`` (HTTP 422) and the server's message: it names the
        key when one key or value breaks a rule, and gives the count when there
        are more than 16 pairs. Labels work on a deployment's address; Comfy
        Cloud refuses them for now with ``code == "metadata_not_supported"``.
        """
        _guard_ui_format(workflow)
        # Validated before any bytes move, like `models.run`: `""` used to
        # fall into the mint-a-fresh-key branch and disable the caller's dedup.
        key = (
            _core.validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else _core.new_idempotency_key()
        )
        graph = self._materialize(workflow)
        extra_data = _core.extra_data_for(api_key)
        deadline = _now() + _QUEUE_RETRY_BUDGET
        # The key is a local of this frame, so anything that propagates past
        # here takes the caller's only record of what was sent with it. The
        # inner handler still owns what is retried and what surfaces; this
        # stamps the key onto whatever it lets out — including a transport
        # failure that never reached a response to translate.
        with translating(idempotency_key=key):
            while True:
                try:
                    model = self._low.post_jobs(
                        graph, idempotency_key=key, extra_data=extra_data, metadata=metadata
                    )
                    return Job(self._low, model)
                except ApiError as exc:
                    err = to_sdk_error(exc)
                    delay = _retry_delay(exc, deadline)
                    if delay is None:
                        raise err from exc
                    time.sleep(delay)
                    continue

    def run(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> Job:
        """Submit, then poll to terminal (authoritative). Raises on failure."""
        job = self.submit(workflow, api_key=api_key)
        return job.result() if timeout is None else _run_with_timeout(job, timeout)

    def list_jobs(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[JobSummary]:
        """Every job this client can see, newest first, one page at a time.

        At a deployment's address that is the deployment's jobs; at the
        workspace address (``TURBINE_BASE_URL=https://platformapi.comfy.org``,
        which serves the job list only), every job in the workspace.

        ``metadata`` keeps only the jobs whose labels include every given
        key with exactly that value. Iteration follows ``next_cursor`` until
        the last page; on a host that pages, ``limit`` is the page size, not a
        cap on the total. The
        server owns the filter rules (how many keys, which characters) and
        refuses a bad filter with :class:`~turbine_sdk.exceptions.TurbineError`
        (``code == "invalid_metadata_filter"``). A cursor the server did not
        issue raises :class:`~turbine_sdk.exceptions.TurbineError` with ``code ==
        "invalid_cursor"``; a page that hands back a cursor this iteration
        already sent, or one that is not a string, raises ``code ==
        "invalid_response"`` rather than being followed.

        The filters are also checked on each item, and an item that does not
        carry every pair is skipped, so a host that ignores the filters yields
        only real matches. On such a host that pages (a gateway without label
        support, for example), one step of the iteration can read several
        pages, or every page, before it yields or ends. A self-hosted proxy
        keeps no labels, so a filtered list there yields nothing rather than
        every job. The proxy reads one page and sends no ``next_cursor``: an
        unfiltered list there yields its newest jobs (50 by default, up to 100
        with ``limit``) and stops.

        Each page retries a 429 that carries ``Retry-After`` the way
        :meth:`submit` does, at the server's pace (at least one second). Each
        page has its own 60-second retry budget, the length :meth:`submit`
        gets, so a long list is not cut short by 429s on earlier pages.

        Comfy Cloud does not list jobs yet: it answers ``code ==
        "not_implemented"`` (HTTP 501). Yields :class:`~turbine_sdk.jobs.JobSummary`
        items; call ``client.jobs.get(summary.id)`` for a full job and its outputs.
        """
        cursor: str | None = None
        sent: set[str] = set()
        while True:
            page = self._list_page(metadata, limit, cursor)
            for item in _page_jobs(page):
                summary = JobSummary._from_item(item)
                if _has_labels(summary, metadata):
                    yield summary
            cursor = _next_list_cursor(page, sent)
            if cursor is None:
                return

    def _list_page(
        self, metadata: Mapping[str, str] | None, limit: int | None, cursor: str | None
    ) -> dict[str, Any]:
        """One page of :meth:`list_jobs`, retrying a paced 429 like :meth:`submit`."""
        deadline = _now() + _QUEUE_RETRY_BUDGET
        with translating():
            while True:
                try:
                    return self._low.list_jobs(metadata=metadata, limit=limit, cursor=cursor)
                except ApiError as exc:
                    delay = _retry_delay(exc, deadline)
                    if delay is None:
                        raise to_sdk_error(exc) from exc
                    time.sleep(delay)


def _run_with_timeout(job: Job, timeout: float) -> Job:
    from ._core import SUCCESS
    from .exceptions import JobFailed

    job.wait(timeout=timeout)
    if job.status != SUCCESS:
        raise JobFailed(f"job {job.id} ended {job.status}", error=job.error)
    return job


class AsyncTurbine:
    """Asynchronous TurbineOS client with the same configuration as :class:`Turbine`."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None | _ApiKeyUnset = _API_KEY_UNSET,
        router_base_url: str | None = None,
        client: httpx2.AsyncClient | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        base_url = _resolve_base_url(base_url)
        key = _resolve_api_key(api_key)
        self._low = AsyncTurbineLow(
            base_url,
            key,
            timeout=timeout,
            client_info=client_info,
            router_base_url=router_base_url,
            client=client,
        )
        self.assets = AsyncAssetFactory(self._low)
        self.workflows = WorkflowFactory()
        self.jobs = AsyncJobFactory(self._low)
        #: Async counterpart of :attr:`Turbine.models`, on this client's transport.
        self.models = AsyncModels(self._low, retry)
        self.turbine = AsyncTurbineExtensions(self._low)

    async def aclose(self) -> None:
        """Async :meth:`Turbine.close`. Prefer ``async with AsyncTurbine(...)``."""
        await self._low.aclose()

    async def __aenter__(self) -> AsyncTurbine:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        """Key-free, exactly as :meth:`Turbine.__repr__`."""
        return (
            f"{type(self).__name__}(base_url={self._low.safe_base_url!r}, "
            f"authenticated={self._low.authenticated})"
        )

    async def _materialize(self, workflow: Workflow) -> dict[str, Any]:
        handles = _core.find_asset_handles(workflow.json)
        refs: dict[int, dict[str, Any]] = {}
        for h in handles:
            await h.commit()
            refs[id(h)] = await h.as_reference()
        return _core.substitute_asset_handles(workflow.json, refs)

    async def submit(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> AsyncJob:
        """Mirrors :meth:`Turbine.submit` — see there for ``api_key`` and ``metadata``."""
        import asyncio

        _guard_ui_format(workflow)
        key = (
            _core.validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else _core.new_idempotency_key()
        )
        graph = await self._materialize(workflow)
        extra_data = _core.extra_data_for(api_key)
        deadline = _now() + _QUEUE_RETRY_BUDGET
        # See :meth:`Turbine.submit` — same stamp, and here it also rides out on
        # the cancellation of an in-flight `post_jobs`.
        with translating(idempotency_key=key):
            while True:
                try:
                    model = await self._low.post_jobs(
                        graph, idempotency_key=key, extra_data=extra_data, metadata=metadata
                    )
                    return AsyncJob(self._low, model)
                except ApiError as exc:
                    err = to_sdk_error(exc)
                    delay = _retry_delay(exc, deadline)
                    if delay is None:
                        raise err from exc
                    await asyncio.sleep(delay)
                    continue

    async def run(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> AsyncJob:
        """Async :meth:`Turbine.run` — submit, then poll to terminal. Raises on failure."""
        from ._core import SUCCESS
        from .exceptions import JobFailed

        job = await self.submit(workflow, api_key=api_key)
        if timeout is None:
            return await job.result()
        await job.wait(timeout=timeout)
        if job.status != SUCCESS:
            raise JobFailed(f"job {job.id} ended {job.status}", error=job.error)
        return job

    async def list_jobs(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[JobSummary]:
        """Async :meth:`Turbine.list_jobs` — ``async for summary in client.list_jobs(...)``."""
        cursor: str | None = None
        sent: set[str] = set()
        while True:
            page = await self._list_page(metadata, limit, cursor)
            for item in _page_jobs(page):
                summary = JobSummary._from_item(item)
                if _has_labels(summary, metadata):
                    yield summary
            cursor = _next_list_cursor(page, sent)
            if cursor is None:
                return

    async def _list_page(
        self, metadata: Mapping[str, str] | None, limit: int | None, cursor: str | None
    ) -> dict[str, Any]:
        """Async :meth:`Turbine._list_page`."""
        import asyncio

        deadline = _now() + _QUEUE_RETRY_BUDGET
        with translating():
            while True:
                try:
                    return await self._low.list_jobs(metadata=metadata, limit=limit, cursor=cursor)
                except ApiError as exc:
                    delay = _retry_delay(exc, deadline)
                    if delay is None:
                        raise to_sdk_error(exc) from exc
                    await asyncio.sleep(delay)
