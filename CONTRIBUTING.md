# Contributing to turbine-python-sdk

Thanks for contributing. This document covers local setup, the checks CI
requires, and the one trap that bites new contributors: `turbine_low`'s models
are generated, and hand-editing them is invisible to the linters but fails CI.

## Prerequisites

- **Python 3.11+** — CI tests 3.11, 3.12, and 3.13.
- **[uv](https://docs.astral.sh/uv/)** — recommended for local work; `uv.lock`
  is committed, so `uv run` gives you the same resolved dependency set as
  everyone else. Plain `pip` works too (see [Using pip instead](#using-pip-instead)).

## Setup

```bash
git clone https://github.com/SystasisIO/turbine-python-sdk
cd turbine-python-sdk
uv sync --extra dev
```

> **`--extra dev` is load-bearing.** The dev tools (ruff, mypy, pytest,
> pytest-asyncio, pytest-cov) are declared in `pyproject.toml` under
> `[project.optional-dependencies]` — a PEP 621 _extra_, not a PEP 735
> `[dependency-groups]` entry. uv installs the default dependency groups
> automatically, but it never installs an extra unless you ask for it. Omit
> `--extra dev` and `ruff`/`mypy`/`pytest` simply will not be there.

## Required checks

Every one of these runs in CI on each pull request and must pass. Run them
locally before pushing:

```bash
uv run --extra dev ruff check .          # lint
uv run --extra dev ruff format --check . # formatting
uv run --extra dev mypy src              # type check
uv run --extra dev pytest -v             # tests
```

`ruff format --check .` only reports; `uv run --extra dev ruff format .` fixes.

A few things that will fail you that are easy to miss:

- **Formatting is a gate, not a suggestion.** CI runs `ruff format --check`.
- **`turbine_sdk` requires full annotations.** `disallow_untyped_defs` is on for
  `turbine_sdk.*` only (it is the hand-written public surface and ships
  `py.typed`). `turbine_low` wraps generated code, so it is exempt.
- **Deprecation warnings are errors.** `filterwarnings` turns
  `DeprecationWarning`, `PendingDeprecationWarning`, and HTTPX2 deprecations into test failures —
  they are the advance warning that a dependency bump is about to break the SDK.
- **Markers and config are strict.** `--strict-markers --strict-config`, so a
  typo'd `@pytest.mark.*` or a bad ini key fails rather than being ignored.

CI runs two more jobs beyond the four above:

- **`build-check`** — builds the sdist and wheel and runs `twine check`, so a
  broken distribution is caught in PR CI instead of at release time.
- **`public-repo-hygiene`** — `python3 scripts/check_public_repo_hygiene.py`
  scans for internal-only references. This is a public repo; the check is a
  permanent gate, not a one-time cleanup.

## The codegen trap: `src/turbine_low/models/_generated.py`

`src/turbine_low/models/_generated.py` is **the only generated file in the repo**.
It is produced by `datamodel-code-generator` from the vendored OpenAPI document
at `spec/openapi.yaml`, and it is committed.

**A hand-edit of that file is invisible to every local check and still fails CI.**
Both linters deliberately skip it — `[tool.ruff] extend-exclude` and
`[tool.mypy] exclude` — so its formatting stays byte-identical to the
generator's output. What catches an edit is the separate **`codegen-drift`** CI
job, which regenerates the file into a temp directory and diffs it byte-for-byte
against the committed copy.

Sync upstream contract changes through the one-way vendoring process in
`spec/README.md`, then regenerate. Do not hand-edit the vendored specification:

```bash
uv run --extra codegen bash scripts/gen_models.sh   # regenerate
uv run --extra codegen python scripts/check_drift.py  # the exact check CI runs
```

Then commit the regenerated `src/turbine_low/models/_generated.py` alongside your
spec change.

Notes:

- `scripts/gen_models.sh` is a **bash** script — run it with `bash`, not `python`.
- `datamodel-code-generator` is pinned (`~=0.68.1`) on purpose. The drift gate
  compares byte-for-byte, so the generator version is load-bearing; an
  unpinned bump would reformat the output and flag false drift. Do not loosen
  that pin without regenerating in the same commit.
- ruff and mypy are pinned for the same class of reason: an unpinned bump
  silently changes what CI catches between PRs.

## Using pip instead

uv is a convenience, not a requirement — CI itself does not use it. The CI test
job installs with pip and then invokes the tools directly:

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest -v
```

and for codegen:

```bash
pip install -e ".[codegen]"
bash scripts/gen_models.sh
python scripts/check_drift.py
```

## Optional extras

| Extra     | What it is for                                                                       |
| --------- | ------------------------------------------------------------------------------------ |
| `dev`     | ruff, mypy, pytest, pytest-asyncio, pytest-cov — everything the required checks need |
| `codegen` | `datamodel-code-generator` + PyYAML, for regenerating `turbine_low` models             |
| `pil`     | Pillow, so `Preview.to_pil()` can decode an in-progress preview frame                |

## Tests

```bash
uv run --extra dev pytest -v                      # the suite CI runs
uv run --extra dev pytest --cov                   # with coverage
uv run --extra dev pytest tests/test_jobs.py -v   # a single file
```

`asyncio_mode = "auto"`, so `async def` tests need no `@pytest.mark.asyncio`.

`tests/integration/` holds a live end-to-end suite against a real gateway. It is
env-gated and skipped unless `TURBINE_BASE_URL` and `TURBINE_API_KEY` are set, so it
does not run in normal CI. If your change touches upload/dedup, submission,
polling, SSE, or output download, running it against a real deployment is worth
the effort — several past releases were verified that way.

## Pull requests

- **Branch from `main`** and open the PR against `main`.
- **Conventional commits.** The history uses `feat:`, `fix:`, `docs:`, `chore:`,
  `ci:`, `test:`, and `feat!:` / `fix!:` for breaking changes. PR titles follow
  the same form — they become the squashed commit message.
- **No AI attribution trailers** in commit messages (no `Co-Authored-By:` for an
  assistant, no "Generated with ..." lines).
- **Review.** Changes require independent review before publication. Maintainers
  must configure verified fork code owners separately; upstream teams are not owners.
- **Update `CHANGELOG.md`.** Add a bullet under `## [Unreleased]` describing the
  user-visible change. Purely internal changes (CI, refactors with no API
  effect) do not need an entry.
- **Update the README** when you change the public surface — it is the primary
  documentation for this SDK.

## Reporting bugs and requesting features

Use the [issue templates](https://github.com/SystasisIO/turbine-python-sdk/issues/new/choose).
For a bug, the SDK version, the Python version, and a minimal reproducible
snippet are what make it actionable.

## Releases

Package-index publication is disabled. The manual `publish.yml` workflow only
builds and validates wheel/sdist artifacts. Actual release requires a separate
maintainer decision, package ownership and Trusted Publisher setup.

Runtime `__version__` comes from installed `turbine-sdk` metadata, not a second
hardcoded version. Keep release metadata and the changelog consistent.
