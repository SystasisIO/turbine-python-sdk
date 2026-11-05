# Compatibility and verification

## Contract ownership

The upstream baseline is `fadbf4c2140bc6eecd54ac2acfde6ff6195af154`.
The vendored `spec/openapi.yaml` declares API version `2.0.0`; its provenance,
Router specification and one-way sync rules are recorded in `spec/README.md`.
The fork's Python names and transport are independent of those wire contracts.

| Surface | Contract source | Sync / async client | Local verification | Live TurbineOS |
| --- | --- | --- | --- | --- |
| Submit, get, workflow, cancel Job | Vendored v2 OpenAPI | Retained public/low APIs | Offline regression suite | NOT_RUN |
| Asset create/hash lookup/get/delete/content | Vendored v2 OpenAPI | Retained public/low APIs | Offline regression suite | NOT_RUN |
| Job event stream | Vendored v2 SSE contract | `job.events()` | Offline stream/reconnect/poll tests | NOT_RUN |
| Job listing and metadata | Existing SDK supplement | `list_jobs`, submit metadata | Offline pagination/filter tests | NOT_RUN |
| Router discovery, model calls and queue | Vendored Router contract and existing SDK | Explicitly enabled `models` resource | Offline Router suite and drift checks | NOT_RUN |
| Native node catalog | Agreed native Comfy object_info response | `client.turbine.nodes.list/get` | Local HTTP stub | NOT_RUN |
| Prepared prompt estimate | Agreed prompt/estimate extension | `client.turbine.estimate_prompt` | Local HTTP stub and shared schema tests | NOT_RUN |

The local stub establishes SDK request/response behavior, not server readiness.
No real provider calls, generations or billing are part of offline acceptance.
Package catalog, financial admission, final charges/refunds and native frontend
HTTP/WebSocket conversion remain outside this SDK iteration.

## Consumer contract

- Install `turbine-sdk`; import `Turbine` / `AsyncTurbine` from `turbine_sdk`.
- Common catalog/graph/pricing types are imported from `turbine_sdk.schemas`.
  They re-export the same low model classes. There is no application/framework
  dependency and schema-only imports create no HTTP clients.
- Existing v2 wire models remain under `turbine_low.models`; Job/Asset handles
  and event helpers remain SDK resources. They do not replace native frontend
  response envelopes or frontend WebSocket messages.
- Estimate takes only prepared JSON. Reuse the same graph for submit; SDK does
  not claim equivalence between native file references and v2 asset references.
- Supply `base_url` or `TURBINE_BASE_URL`. Router is disabled unless explicitly
  configured. No token is automatically sent to Comfy Cloud or Comfy Router.
- Supplied HTTPX2 clients remain caller-owned; SDK-created clients are closed
  by the SDK. No HTTPX alias installation is performed.

## Verification record

Local acceptance completed on 2026-11-05. The full suite was run with four
isolated pytest workers; xdist was installed only in temporary verification
environments and is not an SDK dependency.

| Environment / check | Result |
| --- | --- |
| Python 3.11.18, resolved runtime dependencies | 1448 passed, 9 skipped |
| Python 3.12.11, resolved runtime dependencies | 1448 passed, 9 skipped; one pytest temp-cleanup warning |
| Python 3.13.18, resolved runtime dependencies | 1448 passed, 9 skipped |
| Python 3.11.18, Pydantic 2.6.0 / blake3 0.4.0 / HTTPX2 2.16.0 | 1448 passed, 9 skipped |
| Installed-wheel consumer, Python 3.11 / 3.12 / 3.13 | 120 passed per interpreter |
| Ruff lint / format, mypy | Passed; 28 source files type-checked |
| Generated models and Router error/route/host/media drift | Passed |
| Public repository hygiene and lock consistency | Passed |
| Wheel built from sdist, metadata / py.typed / fixture inventory, twine | Passed |
| Side-by-side upstream baseline comfy-sdk and turbine-sdk | Passed |
| Live TurbineOS | NOT_RUN |

The nine skips are the env-gated live integration cases. No live deployment
was configured and no provider generation was invoked. A clean installed-wheel
environment contained HTTPX2 and no HTTPX; installing the upstream baseline
alongside it brought its separate HTTPX dependency without namespace conflicts.
The tested resolved versions were HTTPX2/httpcore2 2.16.0, Pydantic 2.14.2 and
blake3 1.0.11; the minimum-runtime row checks the declared lower boundaries.

Reproduce the normal local gates from CONTRIBUTING.md. To run the full suite in
parallel, install pytest-xdist in an isolated test environment and use
`python -m pytest -n 4 -q`. CI also installs the built wheel and runs the shared
schema, native-extension and public-consumer tests from a temporary directory
outside the checkout. No editable SDK installation or PYTHONPATH is used there.

## Remaining server dependencies

A live TurbineOS deployment must confirm v2 methods, metadata/list semantics,
SSE and all three native extension routes. A lost submit response needs a
server-supported correlation/recovery contract; v2 key reuse alone is not
response replay. Estimates do not grant financial admission or impose a spend
limit. These conditions do not require introducing a second client in SDK.

## Release state

Wheel/sdist validation is independent of package-index publication. Publishing
is disabled until maintainers approve the package destination, ownership and
Trusted Publisher configuration. Verified fork code owners must be configured
separately; upstream organizational integrations have been removed.
