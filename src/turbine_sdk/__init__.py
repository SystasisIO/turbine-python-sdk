"""Turbine SDK: workflow execution and TurbineOS extensions.

Create ``Turbine(base_url="https://your-deployment.example", api_key="...")``
or use ``TURBINE_BASE_URL`` and ``TURBINE_API_KEY``. Router is opt-in through
``router_base_url``. Both sync and async clients support caller-owned HTTPX2 pools.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .assets import Asset, AssetFactory, AsyncAsset, AsyncAssetFactory
from .client import (
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    AsyncTurbine,
    Turbine,
)
from .events import (
    Event,
    Log,
    OutputReady,
    Preview,
    Progress,
    StatusChange,
)
from .exceptions import (
    AlreadyCompleted,
    BlobNotFound,
    CancelRefused,
    Forbidden,
    HashMismatch,
    IdempotencyKeyReuse,
    InsufficientCredits,
    InvalidWorkflow,
    JobFailed,
    MissingApiKey,
    MissingAsset,
    NotFound,
    QueueFull,
    RouterError,
    TurbineError,
    Unauthorized,
    WorkflowFormatUi,
)
from .jobs import AsyncJob, Job, JobSummary, JobWorkflow
from .model_requests import COMPLETED, AsyncRequestHandle, QueueUpdate, RequestHandle
from .models import BinaryResult
from .outputs import AsyncOutput, DownloadUrl, Output
from .retry import DEFAULT_RETRY, NO_RETRY, RetryPolicy
from .workflows import Workflow, WorkflowFactory

try:
    # Report the installed distribution version, including release build metadata.
    __version__ = _pkg_version("turbine-sdk")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0+unknown"

from .model_catalog import AsyncModelList, CatalogModel, ModelList, ModelPage, SchemaResult
from .models import RouterRunResult

__all__ = [
    # clients
    "Turbine",
    "BASE_URL_ENV_VAR",
    "API_KEY_ENV_VAR",
    "AsyncTurbine",
    # model runs
    "RouterRunResult",
    # model discovery
    "CatalogModel",
    "ModelList",
    "AsyncModelList",
    "ModelPage",
    "SchemaResult",
    # assets / workflows / jobs / outputs
    "Asset",
    "AsyncAsset",
    "AssetFactory",
    "AsyncAssetFactory",
    "Workflow",
    "WorkflowFactory",
    "Job",
    "AsyncJob",
    "JobSummary",
    "JobWorkflow",
    # queued model requests
    "RequestHandle",
    "AsyncRequestHandle",
    "QueueUpdate",
    "COMPLETED",
    "Output",
    "AsyncOutput",
    "DownloadUrl",
    "BinaryResult",
    # events
    "Event",
    "Progress",
    "Preview",
    "OutputReady",
    "StatusChange",
    "Log",
    # exceptions
    "TurbineError",
    "JobFailed",
    "QueueFull",
    "MissingAsset",
    "HashMismatch",
    "InvalidWorkflow",
    "WorkflowFormatUi",
    "BlobNotFound",
    "IdempotencyKeyReuse",
    "InsufficientCredits",
    "NotFound",
    "MissingApiKey",
    "Unauthorized",
    "Forbidden",
    # Comfy Router failures. `RouterError` is the base of every one of them and
    # is lifted here because it is the broad catch a caller writes first; the
    # per-bucket classes stay in `turbine_sdk.router_exceptions`, which is one
    # import path for the whole closed set rather than half of it here and half
    # of it there. `AlreadyCompleted` / `CancelRefused` are the cancel route's
    # refusals, which that closed set does not name.
    "RouterError",
    "CancelRefused",
    "AlreadyCompleted",
    # retry policy
    "RetryPolicy",
    "DEFAULT_RETRY",
    "NO_RETRY",
]
