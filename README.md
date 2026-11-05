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
missed events. GET Job provides authoritative state. A lost submit response
must not be retried with a newly minted identity. Preserve the original key and
error context; server-side recovery remains a contract dependency.

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
`{"prompt": graph}` and does not upload files or submit a workflow. To compare
preview and execution, supply the same prepared graph to both. Equivalence of
native file inputs and v2 asset references must be established by the server.

The shared schemas are defined once in `turbine_low.models` modules and
re-exported as the same types from `turbine_sdk.schemas`. They are suitable for
composition in another Pydantic response model without copying DTO definitions.

```json
{
  "total": {"amount": "48", "precision": 0, "unit": "CREDIT"},
  "nodes": [
    {
      "node_id": "1",
      "model_id": "example-model",
      "price": {"amount": "48", "precision": 0, "unit": "CREDIT"}
    }
  ],
  "breakdown_complete": true
}
```

Money amounts are nonnegative integer strings, without float conversion.
`total` may be null, including when some node prices are known; `model_id` may
be null. Nullable fields are still required. Unknown estimates are never
converted to zero. SDK does not apply markup, convert units or authorize spend.

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
