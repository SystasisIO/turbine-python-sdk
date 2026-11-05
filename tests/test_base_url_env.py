"""Explicit deployment configuration and TURBINE-only environment fallback."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from turbine_sdk import BASE_URL_ENV_VAR, AsyncTurbine, Turbine

LOCAL = "http://127.0.0.1:8189"
CLIENTS = pytest.mark.parametrize("client_cls", [Turbine, AsyncTurbine], ids=["sync", "async"])


@contextmanager
def constructed(client_cls, **kwargs):
    client = client_cls(**kwargs)
    try:
        yield client
    finally:
        if isinstance(client, AsyncTurbine):
            asyncio.run(client.aclose())
        else:
            client.close()


def test_env_var_name() -> None:
    assert BASE_URL_ENV_VAR == "TURBINE_BASE_URL"


@CLIENTS
def test_missing_base_url_is_a_local_error(client_cls) -> None:
    with pytest.raises(ValueError, match="base_url.*TURBINE_BASE_URL"):
        client_cls()


@CLIENTS
def test_comfy_environment_does_not_supply_a_target(monkeypatch, client_cls) -> None:
    monkeypatch.setenv("COMFY_BASE_URL", LOCAL)
    monkeypatch.setenv("COMFY_API_KEY", "unrelated-key")
    with pytest.raises(ValueError, match="TURBINE_BASE_URL"):
        client_cls()


@CLIENTS
def test_explicit_base_url_wins_even_over_an_invalid_environment(monkeypatch, client_cls) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, "invalid-env-value")
    with constructed(client_cls, base_url=LOCAL + "/deployment/") as client:
        assert client._low.base_url == LOCAL + "/deployment"
        assert client._low._p.url("/jobs/j1") == LOCAL + "/deployment/api/v2/jobs/j1"


@CLIENTS
def test_environment_is_read_per_construction(monkeypatch, client_cls) -> None:
    for value in (LOCAL, LOCAL + "/second"):
        monkeypatch.setenv(BASE_URL_ENV_VAR, value)
        with constructed(client_cls) as client:
            assert client._low.base_url == value


@CLIENTS
def test_surrounding_url_whitespace_is_ignored(monkeypatch, client_cls) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, f"  {LOCAL}  ")
    with constructed(client_cls) as client:
        assert client._low.base_url == LOCAL


@CLIENTS
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "deployment.example",
        "ftp://deployment.example",
        "file:///tmp/workflow",
        "http://",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:99999",
        "http://bad host",
        "https://deployment.example?x=1",
        "https://deployment.example?",
        "https://deployment.example#",
        "https://deployment.example?#",
        "https://deployment.example#fragment",
        "not a url",
    ],
)
def test_invalid_explicit_url_is_never_replaced_by_environment(
    monkeypatch, client_cls, bad
) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    with pytest.raises(ValueError, match="base_url"):
        client_cls(base_url=bad)


@CLIENTS
@pytest.mark.parametrize("bad", ["", "   ", "ftp://deployment.example", "http://host:bad"])
def test_invalid_environment_url_is_rejected(monkeypatch, client_cls, bad) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, bad)
    with pytest.raises(ValueError, match=BASE_URL_ENV_VAR):
        client_cls()


@CLIENTS
def test_base_url_remains_keyword_only(client_cls) -> None:
    with pytest.raises(TypeError):
        client_cls(LOCAL)
