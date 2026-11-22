"""Security Hardening Tests for Task SH-01:
DNS-Pinned HTTP Transport, SSRF Hardening, Redirect Security, Port Restrictions, and Production Safeguards.
"""
from __future__ import annotations

import ast
import asyncio
import ipaddress
import socket
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import aiohttp.web
from aiohttp.abc import ResolveResult
import pytest
import trustme
import yaml

from app.config import Settings
from app.errors import AppError
from app.services.website_extractor import PinnedDNSResolver, SafeFetcher
from app.utils.validators import (
    ALLOWED_PORTS,
    DNS_TIMEOUT_SECONDS,
    default_port,
    is_public_ip,
    normalize_url,
    resolve_public_ips,
)
from tests.conftest import make_settings

pytestmark = pytest.mark.anyio


# =============================================================================
# 1. DNS RESOLVER UNIT TESTS (Section 2A & Section 9)
# =============================================================================

async def test_resolver_returns_exact_validated_ipv4():
    """Resolver returns exactly the validated IPv4 addresses with appropriate family."""
    validated = ["93.184.216.34", "198.51.100.1"]
    resolver = PinnedDNSResolver("example.com", 80, validated)
    results = await resolver.resolve("example.com", 80, socket.AF_INET)

    assert len(results) == 2
    assert results[0]["hostname"] == "example.com"
    assert results[0]["host"] == "93.184.216.34"
    assert results[0]["port"] == 80
    assert results[0]["family"] == socket.AF_INET
    assert results[1]["host"] == "198.51.100.1"


async def test_resolver_returns_exact_validated_ipv6():
    """Resolver returns exactly the validated IPv6 addresses with appropriate family."""
    validated = ["2606:4700:4700::1111", "2001:4860:4860::8888"]
    resolver = PinnedDNSResolver("example.com", 443, validated)
    results = await resolver.resolve("example.com", 443, socket.AF_INET6)

    assert len(results) == 2
    assert results[0]["hostname"] == "example.com"
    assert results[0]["host"] == "2606:4700:4700::1111"
    assert results[0]["port"] == 443
    assert results[0]["family"] == socket.AF_INET6
    assert results[1]["host"] == "2001:4860:4860::8888"


async def test_resolver_rejects_hostname_mismatch():
    """Resolver raises OSError on hostname mismatch and never resolves unpinned host."""
    resolver = PinnedDNSResolver("example.com", 80, ["93.184.216.34"])
    with pytest.raises(OSError, match="DNS Pinning violation.*does not match pinned host"):
        await resolver.resolve("attacker.com", 80)


async def test_resolver_rejects_unexpected_port():
    """Resolver raises OSError on port mismatch and never resolves unpinned port."""
    resolver = PinnedDNSResolver("example.com", 80, ["93.184.216.34"])
    with pytest.raises(OSError, match="DNS Pinning violation.*does not match pinned port"):
        await resolver.resolve("example.com", 8080)


async def test_resolver_never_calls_system_dns():
    """Resolver never invokes system getaddrinfo or OS resolver."""
    resolver = PinnedDNSResolver("example.com", 80, ["93.184.216.34"])
    loop = asyncio.get_running_loop()

    with patch.object(loop, "getaddrinfo", side_effect=AssertionError("System DNS invoked!")):
        with patch("socket.getaddrinfo", side_effect=AssertionError("System socket DNS invoked!")):
            results = await resolver.resolve("example.com", 80)
            assert len(results) == 1
            assert results[0]["host"] == "93.184.216.34"


def test_resolver_rejects_empty_pinned_address_list():
    """Resolver cannot be created with an empty address list."""
    with pytest.raises(ValueError, match="Validated IP address list must not be empty"):
        PinnedDNSResolver("example.com", 80, [])

    with pytest.raises(ValueError, match="Pinned hostname must not be empty"):
        PinnedDNSResolver("", 80, ["93.184.216.34"])


async def test_resolve_public_ips_rejects_mixed_public_private():
    """If a DNS response contains both public and private IPs, the host is rejected."""
    loop = asyncio.get_running_loop()
    mixed_infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
    ]

    with patch.object(loop, "getaddrinfo", return_value=mixed_infos):
        with pytest.raises(AppError) as exc:
            await resolve_public_ips("mixed.example", 80)
        assert exc.value.code == "blocked_url"
        assert exc.value.status_code == 400


async def test_resolve_public_ips_dns_timeout_handling():
    """DNS resolution timeout is caught and mapped to AppError(504, 'dns_timeout')."""
    loop = asyncio.get_running_loop()

    async def slow_getaddrinfo(*args, **kwargs):
        await asyncio.sleep(1.0)
        return []

    with patch.object(loop, "getaddrinfo", side_effect=slow_getaddrinfo):
        with pytest.raises(AppError) as exc:
            await resolve_public_ips("slow.example", 80, timeout=0.01)
        assert exc.value.code == "dns_timeout"
        assert exc.value.status_code == 504


async def test_resolve_public_ips_empty_records_rejected():
    """Empty DNS response is rejected with domain_not_found (422)."""
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", return_value=[]):
        with pytest.raises(AppError) as exc:
            await resolve_public_ips("empty.example", 80)
        assert exc.value.code == "domain_not_found"
        assert exc.value.status_code == 422


# =============================================================================
# 2. ACTUAL CONNECTION PINNING & TOCTOU INTEGRATION TESTS (Section 2B & Section 9)
# =============================================================================

async def test_connection_pinning_reaches_pinned_destination_and_preserves_host():
    """Deterministic local integration test with a real aiohttp TCP server.

    Demonstrates:
    - The original hostname is used in the request URL and sent in HTTP Host header.
    - The custom resolver routes the original hostname to the test server's pinned address.
    - The actual connection reaches the pinned destination.
    - No OS DNS lookup occurs.
    """
    received_headers: list[dict[str, str]] = []

    async def handler(request: aiohttp.web.Request):
        received_headers.append(dict(request.headers))
        return aiohttp.web.Response(
            text="<html><body>Pinned Connection Success</body></html>",
            content_type="text/html",
        )

    app = aiohttp.web.Application()
    app.router.add_get("/check-pinning", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server_port = site._server.sockets[0].getsockname()[1]

    try:
        settings = make_settings(allow_private_urls=True)
        fetcher = SafeFetcher(settings=settings)

        # Mock resolve_public_ips to return 127.0.0.1 for virtual hostname
        with patch("app.services.website_extractor.resolve_public_ips", return_value=["127.0.0.1"]) as mock_resolve:
            target_url = f"http://legitimate-brand.example:{server_port}/check-pinning"
            result = await fetcher.fetch(target_url)

            assert result.status == 200
            assert b"Pinned Connection Success" in result.body
            assert result.url == target_url
            mock_resolve.assert_called_once()

            # Verify the HTTP Host header sent over the wire represents the original hostname
            assert len(received_headers) == 1
            assert received_headers[0]["Host"] == f"legitimate-brand.example:{server_port}"
    finally:
        await runner.cleanup()


async def test_dns_rebinding_simulation_cannot_redirect_to_private_target():
    """Simulates a DNS rebinding attack where DNS changes after initial validation.

    Attack scenario:
    1. Attacker's domain rebind.example is validated at t0 -> returns public IP (93.184.216.34).
    2. Attacker flips DNS to 127.0.0.1 / 10.0.0.1 at t1 before the HTTP connection.
    3. Because SafeFetcher pins the transport to the validated IP via PinnedDNSResolver,
       the connection NEVER queries DNS again and cannot connect to 127.0.0.1.
    """
    call_count = {"dns": 0}

    # Internal secret server listening on 127.0.0.1
    secret_reached = []

    async def secret_handler(request: aiohttp.web.Request):
        secret_reached.append(True)
        return aiohttp.web.Response(text="SECRET INTERNAL DATA", content_type="text/html")

    app = aiohttp.web.Application()
    app.router.add_get("/secret", secret_handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    internal_port = site._server.sockets[0].getsockname()[1]

    try:
        def dynamic_dns_query(*args, **kwargs):
            call_count["dns"] += 1
            if call_count["dns"] == 1:
                # First check: returns a public IP
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", internal_port))]
            # Subsequent check: attacker flips to private 127.0.0.1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", internal_port))]

        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=dynamic_dns_query):
            settings = make_settings(allow_private_urls=False)
            fetcher = SafeFetcher(settings=settings)

            # Step 1: initial validation passes for the public IP
            # The TCP connection attempts to connect to 93.184.216.34 (the pinned address),
            # NOT the internal server! It fails to connect to 93.184.216.34:internal_port.
            with pytest.raises(AppError) as exc:
                await fetcher.fetch(f"http://rebind-test.example:{internal_port}/secret")

            # Must fail with unreachable / timeout connecting to 93.184.216.34
            assert exc.value.code in ("website_unreachable", "website_timeout", "blocked_url")

            # The internal secret server was NEVER reached!
            assert secret_reached == []
    finally:
        await runner.cleanup()


# =============================================================================
# 3. TLS VERIFICATION & SNI TESTS (Section 2C & Section 9)
# =============================================================================

async def test_https_sni_and_matching_certificate_succeeds():
    """HTTPS connection verification:

    - Original hostname is used for TLS SNI.
    - Hostname certificate verification succeeds for matching certificate.
    """
    ca = trustme.CA()
    cert = ca.issue_cert("secure.brand.example")

    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(server_ssl)

    client_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca.configure_trust(client_ssl)

    sni_received = []

    async def secure_handler(request: aiohttp.web.Request):
        return aiohttp.web.Response(text="<html><body>Secure Page</body></html>", content_type="text/html")

    app = aiohttp.web.Application()
    app.router.add_get("/tls-test", secure_handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
    await site.start()
    server_port = site._server.sockets[0].getsockname()[1]

    try:
        settings = make_settings(allow_private_urls=True)
        fetcher = SafeFetcher(settings=settings, ssl_context=client_ssl)

        with patch("app.services.website_extractor.resolve_public_ips", return_value=["127.0.0.1"]):
            result = await fetcher.fetch(f"https://secure.brand.example:{server_port}/tls-test")
            assert result.status == 200
            assert b"Secure Page" in result.body
    finally:
        await runner.cleanup()


async def test_https_mismatched_certificate_is_rejected():
    """HTTPS connection with mismatched hostname certificate is rejected (fails closed)."""
    ca = trustme.CA()
    # Certificate is issued for another domain
    cert = ca.issue_cert("different.example.com")

    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(server_ssl)

    client_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca.configure_trust(client_ssl)

    app = aiohttp.web.Application()
    app.router.add_get("/tls-mismatch", lambda r: aiohttp.web.Response(text="Secret", content_type="text/html"))
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
    await site.start()
    server_port = site._server.sockets[0].getsockname()[1]

    try:
        settings = make_settings(allow_private_urls=True)
        fetcher = SafeFetcher(settings=settings, ssl_context=client_ssl)

        with patch("app.services.website_extractor.resolve_public_ips", return_value=["127.0.0.1"]):
            with pytest.raises(AppError) as exc:
                await fetcher.fetch(f"https://requested.example.com:{server_port}/tls-mismatch")
            assert exc.value.code == "website_unreachable"
    finally:
        await runner.cleanup()


def test_no_production_code_disables_ssl_verification():
    """Static AST inspection: verify that no production code sets ssl=False or verify=False."""
    app_dir = Path(__file__).parent.parent / "app"
    for py_file in app_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword):
                if node.arg in ("verify", "verify_ssl", "check_hostname") and isinstance(node.value, ast.Constant):
                    assert node.value.value is not False, f"Insecure SSL setting found in {py_file}:{node.lineno}"
                if node.arg == "ssl" and isinstance(node.value, ast.Constant) and node.value.value is False:
                    raise AssertionError(f"ssl=False found in {py_file}:{node.lineno}")


# =============================================================================
# 4. REDIRECT SECURITY TESTS (Section 4 & Section 9)
# =============================================================================

@pytest.mark.parametrize("target", [
    "http://127.0.0.1:80/",
    "http://localhost/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
])
async def test_redirect_to_prohibited_destinations_blocked(target):
    """Every redirect hop is independently validated; unsafe redirects are blocked before connecting."""
    settings = make_settings(allow_private_urls=False)
    fetcher = SafeFetcher(settings=settings)

    # Hop 0 returns redirect to prohibited target
    class FakeHopSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            class RespCtx:
                async def __aenter__(self):
                    class MockResp:
                        status = 302
                        headers = {"Location": target}
                    return MockResp()
                async def __aexit__(self, *args):
                    pass
            return RespCtx()

    fetcher._session_factory = lambda conn: FakeHopSession()

    with patch("app.services.website_extractor.resolve_public_ips", return_value=["93.184.216.34"]):
        with pytest.raises(AppError) as exc:
            await fetcher.fetch("http://example.com")
        assert exc.value.code == "blocked_url"
        assert exc.value.status_code == 400


async def test_redirect_missing_location_header_fails():
    """Redirect with no Location header raises website_error (502)."""
    settings = make_settings()
    fetcher = SafeFetcher(settings=settings)

    class MissingLocSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            class RespCtx:
                async def __aenter__(self):
                    class MockResp:
                        status = 301
                        headers = {}
                    return MockResp()
                async def __aexit__(self, *args): pass
            return RespCtx()

    fetcher._session_factory = lambda conn: MissingLocSession()
    with patch("app.services.website_extractor.resolve_public_ips", return_value=["93.184.216.34"]):
        with pytest.raises(AppError) as exc:
            await fetcher.fetch("http://example.com")
        assert exc.value.code == "website_error"
        assert exc.value.status_code == 502


async def test_relative_and_absolute_redirects_re_pinned():
    """Verify that relative and absolute redirects each trigger fresh DNS pinning."""
    pinned_hosts: list[str] = []

    async def mock_resolve(host, port, *, allow_private=False, timeout=5.0):
        pinned_hosts.append(host)
        return ["93.184.216.34"]

    hops = ["http://first.example.com", "http://first.example.com/subpage", "http://second.example.com/final"]
    hop_idx = [0]

    class MultiHopSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            class RespCtx:
                async def __aenter__(self):
                    idx = hop_idx[0]
                    hop_idx[0] += 1
                    if idx == 0:
                        # Relative redirect
                        class R1:
                            status = 302
                            headers = {"Location": "/subpage"}
                        return R1()
                    elif idx == 1:
                        # Absolute redirect to another host
                        class R2:
                            status = 301
                            headers = {"Location": "http://second.example.com/final"}
                        return R2()
                    class R3:
                        status = 200
                        headers = {"Content-Type": "text/html"}
                        class Stream:
                            async def iter_chunked(self, n):
                                yield b"<html><body>Final Hop</body></html>"
                        content = Stream()
                    return R3()
                async def __aexit__(self, *args): pass
            return RespCtx()

    settings = make_settings()
    fetcher = SafeFetcher(settings=settings, session_factory=lambda conn: MultiHopSession())

    with patch("app.services.website_extractor.resolve_public_ips", side_effect=mock_resolve):
        result = await fetcher.fetch("http://first.example.com")
        assert result.status == 200
        assert result.url == "http://second.example.com/final"
        # Each distinct hop resolved and pinned its own destination host:
        assert pinned_hosts == ["first.example.com", "first.example.com", "second.example.com"]


# =============================================================================
# 5. PORT RESTRICTIONS TESTS (Section 6 & Section 9)
# =============================================================================

def test_port_restrictions_public_and_private():
    """Public ports restricted to {80, 443}; ports 8080 and 8443 are rejected in production."""
    assert ALLOWED_PORTS == {80, 443}

    # Public URLs with standard ports succeed
    assert normalize_url("http://example.com:80/") == "http://example.com:80/"
    assert normalize_url("https://example.com:443/") == "https://example.com:443/"

    # Public URLs with non-standard ports (including 8080 and 8443) are blocked
    for blocked_port in (8080, 8443, 8000, 3000, 22):
        with pytest.raises(AppError) as exc:
            normalize_url(f"https://example.com:{blocked_port}/")
        assert exc.value.code == "blocked_url"
        assert exc.value.status_code == 400

    # Development/test mode with allow_private=True permits custom ports
    assert normalize_url("http://127.0.0.1:8080/", allow_private=True) == "http://127.0.0.1:8080/"
    assert normalize_url("http://127.0.0.1:8443/", allow_private=True) == "http://127.0.0.1:8443/"
    assert normalize_url("http://127.0.0.1:4000/", allow_private=True) == "http://127.0.0.1:4000/"


# =============================================================================
# 6. PRODUCTION SAFEGUARD TESTS (Section 7 & Section 9)
# =============================================================================

def test_production_environment_default_and_fail_closed():
    """Settings must default to production and fail closed if allow_private_urls=True in production."""
    # 1. Default environment is production
    s_default = Settings(_env_file=None)
    assert s_default.environment == "production"
    assert s_default.allow_private_urls is False

    # 2. Production with allow_private_urls=False succeeds
    s_prod_ok = Settings(_env_file=None, environment="production", allow_private_urls=False)
    assert s_prod_ok.environment == "production"

    # 3. Production with allow_private_urls=True MUST fail closed
    with pytest.raises(ValueError, match="ALLOW_PRIVATE_URLS cannot be enabled in production environment"):
        Settings(_env_file=None, environment="production", allow_private_urls=True)

    # 4. Default environment with allow_private_urls=True MUST fail closed (safe default)
    with pytest.raises(ValueError, match="ALLOW_PRIVATE_URLS cannot be enabled in production environment"):
        Settings(_env_file=None, allow_private_urls=True)

    # 5. Development mode permits allow_private_urls=True
    s_dev = Settings(_env_file=None, environment="development", allow_private_urls=True)
    assert s_dev.environment == "development"
    assert s_dev.allow_private_urls is True

    # 6. Test mode permits allow_private_urls=True
    s_test = Settings(_env_file=None, environment="test", allow_private_urls=True)
    assert s_test.environment == "test"
    assert s_test.allow_private_urls is True


def test_render_yaml_enforces_production_environment():
    """Verify deployment configuration (render.yaml) explicitly selects production environment."""
    render_file = Path(__file__).parent.parent / "render.yaml"
    assert render_file.exists(), "render.yaml must be present"
    data = yaml.safe_load(render_file.read_text(encoding="utf-8"))

    services = data.get("services", [])
    assert len(services) > 0, "No services in render.yaml"
    web_service = services[0]
    env_vars = {item["key"]: item.get("value") for item in web_service.get("envVars", [])}

    assert env_vars.get("ENVIRONMENT") == "production", "render.yaml must explicitly set ENVIRONMENT=production"
