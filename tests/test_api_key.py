"""Credential resolution distinguishes an omitted key from explicit None."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from turbine_sdk import API_KEY_ENV_VAR, AsyncTurbine, Turbine, TurbineError

EXPLICIT = "turbine-explicit"
FROM_ENV = "turbine-from-env"
LOCAL = "http://127.0.0.1:8189"
OMITTED = object()
CLIENTS = pytest.mark.parametrize("client_cls", [Turbine, AsyncTurbine], ids=["sync", "async"])


@contextmanager
def constructed(client_cls, **kwargs):
    client = client_cls(base_url=kwargs.pop("base_url", LOCAL), **kwargs)
    try:
        yield client
    finally:
        if isinstance(client, AsyncTurbine):
            asyncio.run(client.aclose())
        else:
            client.close()


def auth_header(client):
    prepared = client._low._p
    return prepared.headers(prepared.url("/jobs")).get("Authorization")


def test_env_var_name() -> None:
    assert API_KEY_ENV_VAR == "TURBINE_API_KEY"


@CLIENTS
@pytest.mark.parametrize(
    "explicit, env, expected",
    [
        (OMITTED, None, None),
        (OMITTED, FROM_ENV, f"Bearer {FROM_ENV}"),
        (None, FROM_ENV, None),
        (EXPLICIT, FROM_ENV, f"Bearer {EXPLICIT}"),
        (EXPLICIT, None, f"Bearer {EXPLICIT}"),
    ],
)
def test_resolution_order(monkeypatch, client_cls, explicit, env, expected) -> None:
    if env is not None:
        monkeypatch.setenv(API_KEY_ENV_VAR, env)
    kwargs = {} if explicit is OMITTED else {"api_key": explicit}
    with constructed(client_cls, **kwargs) as client:
        assert auth_header(client) == expected
        assert client._low.authenticated is (expected is not None)


@CLIENTS
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_environment_means_no_sdk_key(monkeypatch, client_cls, blank) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, blank)
    with constructed(client_cls) as client:
        assert auth_header(client) is None


@CLIENTS
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_explicit_key_is_rejected_even_with_environment(
    monkeypatch, client_cls, blank
) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with pytest.raises(ValueError, match="api_key.*non-blank"):
        client_cls(base_url=LOCAL, api_key=blank)


@CLIENTS
@pytest.mark.parametrize("source", ["explicit", "environment"])
def test_surrounding_whitespace_is_stripped(monkeypatch, client_cls, source) -> None:
    kwargs = {}
    if source == "explicit":
        kwargs["api_key"] = f"  {EXPLICIT}\n"
    else:
        monkeypatch.setenv(API_KEY_ENV_VAR, f"  {EXPLICIT}\n")
    with constructed(client_cls, **kwargs) as client:
        assert auth_header(client) == f"Bearer {EXPLICIT}"


@CLIENTS
def test_comfy_key_is_not_read(monkeypatch, client_cls) -> None:
    monkeypatch.setenv("COMFY_API_KEY", "unrelated-secret")
    with constructed(client_cls) as client:
        assert auth_header(client) is None


@CLIENTS
@pytest.mark.parametrize("source", ["explicit", "environment"])
def test_key_is_never_in_representations(monkeypatch, client_cls, source) -> None:
    kwargs = {}
    if source == "explicit":
        kwargs["api_key"] = EXPLICIT
    else:
        monkeypatch.setenv(API_KEY_ENV_VAR, EXPLICIT)
    with constructed(client_cls, **kwargs) as client:
        for rendered in (repr(client), str(client), repr(client._low), repr(client._low._p)):
            assert EXPLICIT not in rendered
        assert "authenticated=True" in repr(client)


@CLIENTS
def test_keyless_client_reports_unauthenticated(client_cls) -> None:
    with constructed(client_cls) as client:
        assert "authenticated=False" in repr(client)


@CLIENTS
@pytest.mark.parametrize("base_url", ["https://cloud.comfy.org", "https://CLOUD.Comfy.ORG:443/"])
def test_an_explicit_host_has_no_cloud_specific_key_requirement(client_cls, base_url) -> None:
    with constructed(client_cls, base_url=base_url) as client:
        assert auth_header(client) is None


@CLIENTS
def test_url_credentials_are_redacted_in_every_repr(client_cls) -> None:
    secret = "proxy-secret"
    url = f"https://proxy-user:{secret}@proxy.example"
    with constructed(client_cls, base_url=url, router_base_url=url) as client:
        for rendered in (
            repr(client),
            str(client),
            repr(client._low),
            repr(client._low._p),
            repr(client.models),
        ):
            assert secret not in rendered
            assert "proxy-user" not in rendered
            assert "***@proxy.example" in rendered
        assert client._low.base_url == client._low.router_base_url == url


@CLIENTS
def test_invalid_url_does_not_echo_embedded_credentials(client_cls) -> None:
    secret = "proxy-secret"
    with pytest.raises(ValueError) as exc:
        client_cls(base_url=f"ftp://user:{secret}@proxy.example")
    assert secret not in str(exc.value)


def test_environment_key_reaches_the_server(server, monkeypatch) -> None:
    server.state.require_auth = True
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with Turbine() as client:
        job = client.submit(client.workflows.from_json({"3": {"class_type": "K", "inputs": {}}}))
        job.refresh()
    assert server.state.last_auth_header == f"Bearer {FROM_ENV}"


def test_explicit_none_sends_no_sdk_bearer_despite_environment(server, monkeypatch) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with Turbine(api_key=None) as client:
        client._low.get_job("job_01")
    assert server.state.last_auth_header == ""


def test_key_is_never_in_a_rejected_request_error(server) -> None:
    server.state.job_error = (401, "unauthorized")
    with Turbine(api_key=EXPLICIT) as client:
        with pytest.raises(TurbineError) as exc:
            client.submit(client.workflows.from_json({"3": {"class_type": "K", "inputs": {}}}))
    assert server.state.last_auth_header == f"Bearer {EXPLICIT}"
    assert EXPLICIT not in str(exc.value)
    assert EXPLICIT not in repr(exc.value)
