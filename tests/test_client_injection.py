"""Caller-owned HTTPX2 pools retain settings and isolate download credentials."""

from __future__ import annotations

import asyncio
import ssl
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest

from turbine_low.transport import _SSE_IDLE_TIMEOUT, MODEL_RUN_TIMEOUT
from turbine_sdk import AsyncTurbine, Turbine


def test_sdk_owns_its_default_client(server) -> None:
    with Turbine() as sdk:
        pool = sdk._low._client
        assert sdk._low.timeout == httpx2.Timeout(30)
        assert not pool.is_closed
    assert pool.is_closed


async def test_async_sdk_owns_its_default_client(server) -> None:
    async with AsyncTurbine() as sdk:
        pool = sdk._low._client
        assert sdk._low.timeout == httpx2.Timeout(30)
        assert not pool.is_closed
    assert pool.is_closed


def test_borrowed_pool_settings_and_ownership_are_preserved(server) -> None:
    requests = []
    auth = httpx2.BasicAuth("caller", "password")
    with httpx2.Client(
        headers={"X-Caller": "retained", "Authorization": "caller-header"},
        auth=auth,
        timeout=7.5,
        follow_redirects=False,
        event_hooks={"request": [requests.append]},
    ) as pool:
        before = (dict(pool.headers), pool.auth, pool.timeout, pool.follow_redirects)
        with Turbine(client=pool, timeout=0.001, api_key=None) as sdk:
            assert sdk._low._client is pool
            sdk._low.get_job("job_01")
        sdk.close()
        assert not pool.is_closed
        assert (dict(pool.headers), pool.auth, pool.timeout, pool.follow_redirects) == before
        assert requests[-1].headers["X-Caller"] == "retained"
        assert requests[-1].extensions["timeout"]["read"] == 7.5
        assert server.state.last_auth_header.startswith("Basic ")
        assert pool.get(server.base_url + "/api/v2/jobs/job_01").status_code == 200


async def test_async_borrowed_pool_settings_and_ownership_are_preserved(server) -> None:
    requests = []

    async def observe(request):
        requests.append(request)

    async with httpx2.AsyncClient(
        headers={"X-Caller": "retained", "Authorization": "caller-header"},
        auth=("caller", "password"),
        timeout=7.5,
        follow_redirects=False,
        event_hooks={"request": [observe]},
    ) as pool:
        before = (dict(pool.headers), pool.auth, pool.timeout, pool.follow_redirects)
        async with AsyncTurbine(client=pool, timeout=0.001, api_key=None) as sdk:
            assert sdk._low._client is pool
            await sdk._low.get_job("job_01")
        await sdk.aclose()
        assert not pool.is_closed
        assert (dict(pool.headers), pool.auth, pool.timeout, pool.follow_redirects) == before
        assert requests[-1].headers["X-Caller"] == "retained"
        assert requests[-1].extensions["timeout"]["read"] == 7.5
        assert server.state.last_auth_header.startswith("Basic ")
        assert (await pool.get(server.base_url + "/api/v2/jobs/job_01")).status_code == 200


def test_explicit_sdk_key_cannot_be_replaced_by_borrowed_default_auth(server) -> None:
    with httpx2.Client(
        headers={"Authorization": "caller-header"}, auth=("caller", "secret")
    ) as pool:
        with Turbine(client=pool, api_key="sdk-key") as sdk:
            sdk._low.get_job("job_01")
        assert server.state.last_auth_header == "Bearer sdk-key"
        assert pool.headers["Authorization"] == "caller-header"


async def test_async_explicit_sdk_key_cannot_be_replaced_by_borrowed_default_auth(server) -> None:
    async with httpx2.AsyncClient(
        headers={"Authorization": "caller-header"}, auth=("caller", "secret")
    ) as pool:
        async with AsyncTurbine(client=pool, api_key="sdk-key") as sdk:
            await sdk._low.get_job("job_01")
        assert server.state.last_auth_header == "Bearer sdk-key"
        assert pool.headers["Authorization"] == "caller-header"


def test_follow_up_url_suppresses_inherited_headers_and_auth(server, second_server) -> None:
    with httpx2.Client(
        headers={"Authorization": "caller-header"}, auth=("caller", "secret")
    ) as pool:
        with Turbine(client=pool, api_key="sdk-key") as sdk:
            sdk._low.get_job(second_server.base_url + "/api/v2/jobs/job_01")
        assert second_server.state.last_auth_header == ""
        assert pool.headers["Authorization"] == "caller-header"


async def test_async_follow_up_url_suppresses_inherited_headers_and_auth(
    server, second_server
) -> None:
    async with httpx2.AsyncClient(
        headers={"Authorization": "caller-header"}, auth=("caller", "secret")
    ) as pool:
        async with AsyncTurbine(client=pool, api_key="sdk-key") as sdk:
            await sdk._low.get_job(second_server.base_url + "/api/v2/jobs/job_01")
        assert second_server.state.last_auth_header == ""


def test_from_url_uses_borrowed_pool_with_redirects_and_without_any_authorization(
    server,
    second_server,
) -> None:
    seen = []
    server.state.redirect_content_to = second_server.base_url + "/api/v2/assets/out/content"
    second_server.state.content_bytes = b"downloaded-source"
    with httpx2.Client(
        headers={"Authorization": "caller-header"},
        auth=("caller", "secret"),
        follow_redirects=False,
        event_hooks={"response": [seen.append]},
    ) as pool:
        with Turbine(client=pool, api_key="sdk-key") as sdk:
            asset = sdk.assets.from_url(server.base_url + "/api/v2/assets/source/content")
            assert server.state.last_auth_header == second_server.state.last_auth_header == ""
            assert [response.status_code for response in seen] == [302, 200]
            assert all(response.is_closed for response in seen)
            assert all("Authorization" not in response.request.headers for response in seen)
            assert server.state.upload_count == 0
            asset.commit()
            assert b"downloaded-source" in server.state.last_upload_body
            assert server.state.last_auth_header == "Bearer sdk-key"
        assert pool.follow_redirects is False
        assert not pool.is_closed


async def test_async_from_url_uses_borrowed_pool_and_suppresses_all_authorization(
    server,
    second_server,
) -> None:
    seen = []

    async def observe(response):
        seen.append(response)

    server.state.redirect_content_to = second_server.base_url + "/api/v2/assets/out/content"
    second_server.state.content_bytes = b"downloaded-source"
    async with httpx2.AsyncClient(
        headers={"Authorization": "caller-header"},
        auth=("caller", "secret"),
        follow_redirects=False,
        event_hooks={"response": [observe]},
    ) as pool:
        async with AsyncTurbine(client=pool, api_key="sdk-key") as sdk:
            asset = await sdk.assets.from_url(server.base_url + "/api/v2/assets/source/content")
            assert server.state.last_auth_header == second_server.state.last_auth_header == ""
            assert [response.status_code for response in seen] == [302, 200]
            assert all(response.is_closed for response in seen)
            assert all("Authorization" not in response.request.headers for response in seen)
            assert server.state.upload_count == 0
            await asset.commit()
            assert b"downloaded-source" in server.state.last_upload_body
            assert server.state.last_auth_header == "Bearer sdk-key"
        assert not pool.is_closed


def test_content_download_overrides_redirect_default_but_signed_url_resolution_does_not(
    server,
    second_server,
) -> None:
    seen = []
    redirect = second_server.base_url + "/api/v2/assets/out/content"
    server.state.redirect_content_to = redirect
    with httpx2.Client(
        auth=("caller", "secret"),
        follow_redirects=False,
        event_hooks={"response": [seen.append]},
    ) as pool:
        with Turbine(client=pool, api_key="sdk-key") as sdk:
            assert sdk._low.get_asset_content_url("out")[0] == redirect
            assert [response.status_code for response in seen] == [302]
            assert all(response.is_closed for response in seen)
            with sdk._low.get_asset_content("out") as response:
                assert response.read() == second_server.state.content_bytes
            assert second_server.state.last_auth_header == ""
            assert all(response.is_closed for response in seen)
        assert pool.follow_redirects is False


async def test_async_content_download_overrides_redirect_default_and_closes_every_response(
    server,
    second_server,
) -> None:
    seen = []

    async def observe(response):
        seen.append(response)

    redirect = second_server.base_url + "/api/v2/assets/out/content"
    server.state.redirect_content_to = redirect
    async with httpx2.AsyncClient(
        auth=("caller", "secret"),
        follow_redirects=False,
        event_hooks={"response": [observe]},
    ) as pool:
        async with AsyncTurbine(client=pool, api_key="sdk-key") as sdk:
            assert (await sdk._low.get_asset_content_url("out"))[0] == redirect
            assert [response.status_code for response in seen] == [302]
            async with sdk._low.get_asset_content("out") as response:
                assert await response.aread() == second_server.state.content_bytes
            assert second_server.state.last_auth_header == ""
            assert all(response.is_closed for response in seen)
        assert pool.follow_redirects is False


def test_special_timeouts_remain_request_scoped(server) -> None:
    seen = []
    with httpx2.Client(timeout=0.1, event_hooks={"request": [seen.append]}) as pool:
        with Turbine(client=pool, router_base_url=server.base_url) as sdk:
            sdk.models.run("bfl/flux-2-pro", {"prompt": "test"})
            assert seen[-1].extensions["timeout"]["read"] == MODEL_RUN_TIMEOUT.read
            list(sdk._low.get_job_events("job_01"))
            assert seen[-1].extensions["timeout"]["read"] == _SSE_IDLE_TIMEOUT.read
            assert pool.timeout.read == 0.1


def test_stream_closes_when_the_consumer_raises(server) -> None:
    with Turbine() as sdk:
        response = None
        with pytest.raises(RuntimeError, match="consumer failed"):
            with sdk._low.open("GET", "/jobs/job_01") as response:
                raise RuntimeError("consumer failed")
        assert response is not None and response.is_closed


async def test_cancelling_an_sse_consumer_closes_stream_and_releases_borrowed_pool(server) -> None:
    seen = []
    opened = asyncio.Event()
    server.state.sse_mode = "stall"
    server.state.stall_seconds = 0.2

    async def observe(response):
        seen.append(response)
        opened.set()

    async with httpx2.AsyncClient(
        limits=httpx2.Limits(max_connections=1),
        timeout=1,
        event_hooks={"response": [observe]},
    ) as pool:
        async with AsyncTurbine(client=pool) as sdk:

            async def consume():
                async for _ in sdk._low.get_job_events("job_01"):
                    pass

            task = asyncio.create_task(consume())
            await asyncio.wait_for(opened.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert seen[0].is_closed
            # A leaked stream would exhaust this one-connection pool.
            assert (await sdk._low.get_job("job_01")).id == "job_01"
        assert not pool.is_closed


@pytest.fixture
def auth_refresh_server():
    """A same-origin redirect whose target requires a fresh auth challenge."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/api/v2/assets/out/content":
                self.send_response(302)
                self.send_header("Location", "/actual")
                body = b""
            elif self.headers.get("Authorization") != "Bearer fresh":
                self.send_response(401)
                body = b"challenge"
            else:
                self.send_response(200)
                body = b"authenticated-content"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


class RefreshAuth(httpx2.Auth):
    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer stale"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = "Bearer fresh"
            yield request


def test_same_origin_redirect_preserves_borrowed_custom_auth_challenges(
    auth_refresh_server,
) -> None:
    base_url, seen = auth_refresh_server
    with httpx2.Client(auth=RefreshAuth(), follow_redirects=False) as pool:
        with Turbine(base_url=base_url, client=pool, api_key=None) as sdk:
            with sdk._low.get_asset_content("out") as response:
                assert response.read() == b"authenticated-content"
    assert seen == [
        ("/api/v2/assets/out/content", "Bearer stale"),
        ("/actual", "Bearer stale"),
        ("/actual", "Bearer fresh"),
    ]


async def test_async_same_origin_redirect_preserves_borrowed_auth_challenges(
    auth_refresh_server,
) -> None:
    base_url, seen = auth_refresh_server
    async with httpx2.AsyncClient(auth=RefreshAuth(), follow_redirects=False) as pool:
        async with AsyncTurbine(base_url=base_url, client=pool, api_key=None) as sdk:
            async with sdk._low.get_asset_content("out") as response:
                assert await response.aread() == b"authenticated-content"
    assert seen[-1] == ("/actual", "Bearer fresh")


def test_sync_client_rejects_a_different_client_type(server) -> None:
    with pytest.raises(TypeError, match="httpx2.Client"):
        Turbine(client=object())


async def test_async_client_rejects_a_different_client_type(server) -> None:
    async with httpx2.AsyncClient() as pool:
        with pytest.raises(TypeError, match="httpx2.Client"):
            Turbine(client=pool)
    with httpx2.Client() as pool:
        with pytest.raises(TypeError, match="httpx2.AsyncClient"):
            AsyncTurbine(client=pool)


@pytest.fixture
def tls_server():
    """A local HTTPS endpoint with a public, test-only certificate and key."""
    directory = Path(__file__).parent / "certificates"
    certificate = directory / "localhost-test-cert.pem"
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization", ""))
            body = b"tls-protected-content"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, directory / "localhost-test-key.pem")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{httpd.server_port}", certificate, seen
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_default_client_verifies_tls_and_borrowed_client_keeps_its_trust_settings(
    tls_server,
) -> None:
    url, certificate, seen = tls_server
    with Turbine(base_url=url) as sdk:
        with pytest.raises(httpx2.ConnectError):
            sdk.assets.from_url(url + "/source")
    trusted = ssl.create_default_context(cafile=str(certificate))
    assert trusted.verify_mode == ssl.CERT_REQUIRED and trusted.check_hostname
    with httpx2.Client(verify=trusted) as pool:
        with Turbine(base_url=url, client=pool, api_key="sdk-key") as sdk:
            assert sdk._low.raw_request("GET", "/probe").content == b"tls-protected-content"
            assert sdk.assets.from_url(url + "/source").file_path == "source"
        assert seen == ["Bearer sdk-key", ""]
        assert pool.get(url + "/after-close").content == b"tls-protected-content"


async def test_async_default_tls_verification_and_borrowed_custom_ca(tls_server) -> None:
    url, certificate, seen = tls_server
    async with AsyncTurbine(base_url=url) as sdk:
        with pytest.raises(httpx2.ConnectError):
            await sdk.assets.from_url(url + "/source")
    trusted = ssl.create_default_context(cafile=str(certificate))
    assert trusted.verify_mode == ssl.CERT_REQUIRED and trusted.check_hostname
    async with httpx2.AsyncClient(verify=trusted) as pool:
        async with AsyncTurbine(base_url=url, client=pool, api_key="sdk-key") as sdk:
            response = await sdk._low.raw_request("GET", "/probe")
            assert response.content == b"tls-protected-content"
            assert (await sdk.assets.from_url(url + "/source")).file_path == "source"
        assert seen == ["Bearer sdk-key", ""]
        assert (await pool.get(url + "/after-close")).content == b"tls-protected-content"


def test_httpx2_deprecation_warnings_remain_errors() -> None:
    from httpx2._exceptions import HTTPXDeprecationWarning

    assert issubclass(HTTPXDeprecationWarning, UserWarning)
    with pytest.raises(HTTPXDeprecationWarning):
        warnings.warn("deprecated HTTPX2 API", HTTPXDeprecationWarning, stacklevel=1)


def test_redirect_limit_closes_all_responses_and_keeps_borrowed_pool_open(server) -> None:
    seen = []
    server.state.redirect_content_to = server.base_url + "/api/v2/assets/out/content"
    with httpx2.Client(max_redirects=2, event_hooks={"response": [seen.append]}) as pool:
        with Turbine(client=pool) as sdk:
            with pytest.raises(httpx2.TooManyRedirects):
                with sdk._low.get_asset_content("out"):
                    pytest.fail("a redirect loop cannot produce asset content")
            assert len(seen) == 3
            assert all(response.is_closed for response in seen)
        assert not pool.is_closed


async def test_async_redirect_limit_closes_all_responses(server) -> None:
    seen = []

    async def observe(response):
        seen.append(response)

    server.state.redirect_content_to = server.base_url + "/api/v2/assets/out/content"
    async with httpx2.AsyncClient(max_redirects=2, event_hooks={"response": [observe]}) as pool:
        async with AsyncTurbine(client=pool) as sdk:
            with pytest.raises(httpx2.TooManyRedirects):
                async with sdk._low.get_asset_content("out"):
                    pytest.fail("a redirect loop cannot produce asset content")
            assert len(seen) == 3
            assert all(response.is_closed for response in seen)
        assert not pool.is_closed
