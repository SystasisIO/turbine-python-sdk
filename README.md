# turbine-python-sdk

Python SDK for TurbineOS, derived from the MIT-licensed
[Comfy Python SDK](https://github.com/Comfy-Org/comfy-python-sdk).
It preserves Comfy API v2 workflow execution, jobs, assets, outputs and SSE,
and adds node discovery and prompt cost estimates.

Requires **Python 3.11+**. Runtime dependencies: `httpx2`, `blake3`, Pydantic v2.
See the [adaptation plan](docs/turbine-adaptation-plan.md) and
[compatibility and verification record](docs/compatibility.md).

## Install

Until a package-index release is configured, install a built wheel or a pinned
commit from this repository. Distribution name: `turbine-sdk`.

```bash
pip install /path/to/turbine_sdk-0.1.0-py3-none-any.whl
# Developing this repository:
pip install -e '.[dev]'
```

Imports are `turbine_sdk` and `turbine_low`; clients are `Turbine` and
`AsyncTurbine`. There are no compatibility alias packages. The upstream
`comfy-sdk` can be installed alongside this distribution.

## Configuration

```python
from turbine_sdk import Turbine

with Turbine(base_url="http://127.0.0.1:8189", api_key=None) as client:
    nodes = client.turbine.nodes.list()
```

| Argument | Resolution |
| --- | --- |
| `base_url` | Explicit URL, otherwise `TURBINE_BASE_URL`; required. No Cloud fallback. |
| `api_key` | Omitted: `TURBINE_API_KEY`; explicit `None`: no SDK bearer token. Explicit empty strings are rejected. |
| `router_base_url` | Explicit URL only; `None` disables Router. |
| `client` | Optional `httpx2.Client` / `httpx2.AsyncClient` of the matching kind. |

Invalid explicit URLs are rejected instead of falling back to environment.
Blank key environment values mean no key. `COMFY_*` variables are not read.
Router is opt-in and uses this instance's key; select its address and credential
explicitly. The retained Router catalog is separate from native node discovery.

```python
import httpx2
from turbine_sdk import AsyncTurbine

async def discover() -> None:
    async with httpx2.AsyncClient(timeout=20.0) as http_client:
        async with AsyncTurbine(
            base_url="http://127.0.0.1:8189",
            api_key=None,
            client=http_client,
        ) as sdk:
            await sdk.turbine.nodes.list()
        # http_client still belongs to this outer context.
```

SDK-created clients are closed by the SDK. Supplied clients are not closed or
mutated globally. Their ordinary timeout is used; stream/Router requests keep
operation-specific timeouts. Content downloads enable redirects per request;
signed-URL discovery does not. Credentials are scoped to configured API origins,
not arbitrary asset URLs. Use HTTPX2 clients and exceptions; HTTPX objects are
not interchangeable. No global import aliasing is performed.

## Workflow execution

```python
from turbine_sdk import Turbine

with Turbine(base_url="http://127.0.0.1:8189", api_key=None) as client:
    workflow = client.workflows.from_file("workflow_api.json")
    image = client.assets.from_file("photo.png")
    workflow.set_input("10", "image", image)
    job = client.run(workflow)
    for output in job.get_outputs("9"):
        output.to_file(output.name)
```

Workflows can also be constructed with `workflows.from_json(graph)` or
`workflows.from_str(text)`. Asset handles are uploaded/deduplicated and replaced
by `core/ASSET` references during submit. `submit(workflow)` returns a Job;
`run(workflow)` waits for completion. Retained methods include `jobs.get(id)`,
`list_jobs(...)`, `job.wait()`, `job.cancel()`, `job.get_workflow()`,
`job.get_outputs(node_id)` and output download helpers.

```python
from turbine_sdk import AsyncTurbine

async def generate() -> None:
    async with AsyncTurbine(base_url="http://127.0.0.1:8189", api_key=None) as client:
        workflow = client.workflows.from_file("workflow_api.json")
        job = await client.submit(workflow)
        async for event in job.events():
            print(event)
        await job.wait()
        for output in job.get_outputs("9"):
            await output.to_file(output.name)
```

SSE reconnect follows the existing v2 behavior; it does not promise replay of
missed events. GET Job provides authoritative execution state. Preserve the
original key and error context after a lost submit response. See paid workflow
submission for recovery by job metadata.

## Nodes and estimates

| Method (sync and async) | Server route |
| --- | --- |
| `client.turbine.nodes.list()` | `GET /api/object_info` |
| `client.turbine.nodes.get(class_type)` | `GET /api/object_info/{class_type}` |
| `client.turbine.estimate_prompt(prompt)` | `POST /api/prompt/estimate` |

Both node methods return the native mapping from class name to its definition.
Inputs, widget descriptors, ordering and additional node metadata are preserved.
These routes share the v2 client's pool and authorization, with native paths.

```python
from turbine_sdk import Turbine
from turbine_sdk.schemas import EstimateReply, Money

with Turbine(base_url="http://127.0.0.1:8189", api_key=None) as client:
    graph = {"1": {"class_type": "ExampleNode", "inputs": {}}}
    estimate = client.turbine.estimate_prompt(graph)
    if estimate.total is not None:
        print(estimate.total.amount, estimate.total.precision, estimate.total.unit)
```

Estimate accepts **prepared JSON only**, without Asset handles. It sends only
`{"prompt": graph}` and does not upload files or submit a workflow. For v2 asset
inputs, call `asset.as_reference()` (or `await asset.as_reference()`) first.
Place the resulting `core/ASSET` object in the graph and pass the same graph
to estimate and submit. Turbine verifies the asset's owner and media type.

The shared schemas are defined once in `turbine_low.models` modules and
re-exported as the same types from `turbine_sdk.schemas`. They are suitable for
composition in another Pydantic response model without copying DTO definitions.

```json
{
  "total": {"amount": "48", "precision": 0, "unit": "CREDIT"},
  "nodes": [
    {
      "node_id": "1",
      "package_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "operation_id": "text.complete",
      "model_id": "example-model",
      "price": {"amount": "48", "precision": 0, "unit": "CREDIT"}
    }
  ],
  "breakdown_complete": true
}
```

Money amounts are nonnegative integer strings. The SDK returns them in the
server's unit and precision. `unit` and `node_id` are nonempty strings.
Each price row requires `package_hash`, the 64-character lowercase SHA-256
digest from the catalog's `reference.digest`, and `operation_id`, the resolved
operation's ID within that release. `model_id` is a nonempty string for a
selected or fixed model and `null` for a binding without a model. The `total`
field may be `null` even when some node prices are known. Nullable fields are
still required. A row missing either identity field raises
`invalid_schema_response`.

## Paid workflow submission

The SDK requests pricing terms with the `X-Turbine-Pricing: 1` header.
Callers that omit the header receive the original `total`, `nodes` and
`breakdown_complete` fields. An older server can return this original
response to the SDK, in which case `estimate.pricing` is `None`.

When `estimate.pricing` is present, it carries a spending limit for the graph
and each selected node. Each limit specifies the token contract, unit and
precision. The terms identify the release, operation, binding, model and exact
tariff, including the digest of its input mapping. Turbine verifies the
current terms and price before admitting the job. [Pricing extension OpenAPI](spec/turbine-pricing.openapi.yaml)

```python
workflow = client.workflows.from_json(graph)
estimate = client.turbine.estimate_prompt(workflow.json)
if estimate.pricing is None:
    raise ValueError("A complete price estimate is required")

# Use the canonical UUID stored for this logical submission.
job = client.submit(workflow, pricing=estimate.pricing, idempotency_key=submission_id)
```

For paid submission, omit the optional partner-node `api_key` argument to
`submit`. The client authenticates with its Turbine deployment credential.

A `201` response confirms admission. `job.status == "succeeded"` confirms that
the workflow ran successfully. The estimate and submitted `pricing` show the
spending limits. The deployment tracks final charges and refunds separately
from the SDK's Job responses.

The SDK sends the UUID in `Idempotency-Key` and in `metadata.backend_job_id`.
On `409 payment_terms_mismatch` or `409 payment_budget_exceeded`, obtain a fresh
estimate and authorize its terms before submitting again. After a lost 201,
look up the stored UUID using the same deployment and credential. An
accepted key reused on POST receives `422 idempotency_key_reuse`.

```python
labels = {"backend_job_id": submission_id}
matches = list(client.list_jobs(metadata=labels))
if len(matches) == 1:
    recovered = client.jobs.get(matches[0].id)
    if recovered.id == matches[0].id and recovered.metadata.get("backend_job_id") == submission_id:
        # Continue with the original job.
        job = recovered
```

Keep an ambiguous submission pending when you cannot confirm one exact match
and its full Job. The async client uses the same authorization and lookup
with `await` and `async for`.

## Service package catalog

| Method (sync and async) | Result |
| --- | --- |
| `client.turbine.packages.snapshot()` | `CatalogSnapshot` |
| `client.turbine.packages.list(catalog_revision=..., cursor=None, limit=100)` | `PackagePage` |
| `client.turbine.packages.get(package_hash, catalog_revision=...)` | `Package` |

Configure `TURBINE_BASE_URL` and `TURBINE_API_KEY` with the deployment URL and a
service Bearer token that grants `catalog:read`. All three methods use the
client's HTTP pool and timeout. They send requests under `/api/broker/v1/catalog`
on the configured deployment, preserving its path prefix and stopping at
redirect responses.

```python
from turbine_sdk import Turbine

with Turbine() as client:
    packages = client.turbine.packages
    snapshot = packages.snapshot()
    page = packages.list(catalog_revision=snapshot.revision)
    if page.data:
        package = packages.get(page.data[0].reference.digest, catalog_revision=snapshot.revision)
```

Each `list()` call reads one page with a `limit` from 1 to 200. Use the same
revision and the preceding page's `next_cursor` to continue until it is `null`.
If `list()` or `get()` raises `CatalogSnapshotExpired`, discard the incomplete
traversal and start at the first page of a fresh snapshot. The SDK raises this
exception for `410 catalog_snapshot_expired`.

The response types and their nested types are importable from
`turbine_sdk.schemas`. The models validate package format 1.1 and preserve
binding-specific input and operation output schemas as JSON objects. A model
without a provider-guaranteed version has `version: null`. Catalog errors carry
HTTP status, the server code, request ID and retry delay when present.

## Errors and optional features

The public exception base is `TurbineError`. Existing typed v2 and Router errors
retain their meaning; transport failures remain HTTPX2 exceptions. Native
extension errors preserve HTTP status and safe protocol details. Malformed
JSON and invalid response shapes are client errors, not successful estimates.
SDK exceptions and representations must not expose keys.

Router's `models` functionality is retained, but needs an explicit
`router_base_url` and a credential suitable for that endpoint. Workflow partner
node credentials remain protocol body fields distinct from the SDK's bearer.
Optional `pil` installs Pillow for preview decoding. Existing retry policies,
Router schemas and advanced APIs remain in the source with typed interfaces.

## Development and releases

See [CONTRIBUTING.md](CONTRIBUTING.md). Vendored upstream specifications remain
one-way inputs; generated v2 models must be regenerated with the pinned tool,
not edited by hand. New native schemas do not modify the upstream v2 operation set.

Package-index publication is disabled. The manual release workflow only builds
and validates artifacts. Configuring project ownership, verified code owners
and a Trusted Publisher is a separate maintainer action. MIT attribution and
upstream provenance are retained in [LICENSE](LICENSE).
