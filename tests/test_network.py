import asyncio
import socket

import httpx
import pytest

from bibvault import network
from bibvault.errors import UnsafeURLError
from bibvault.network import HostLimiter, guard_request, is_public_http_url

REAL_RESOLVE = network._resolve


def guard(url: str) -> None:
    asyncio.run(guard_request(httpx.Request("GET", url)))


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.7"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fe80::1%en0"],
        ["::ffff:127.0.0.1"],
        ["93.184.216.34", "192.168.1.20"],
    ],
)
def test_hosts_that_resolve_to_local_addresses_are_refused(dns, addresses):
    dns["papers.intranet.example"] = addresses
    with pytest.raises(UnsafeURLError, match="resolves to"):
        guard("https://papers.intranet.example/paper.pdf")


def test_hosts_that_resolve_publicly_are_allowed(dns):
    dns["arxiv.org"] = ["151.101.3.42", "2a04:4e42::347"]
    guard("https://arxiv.org/pdf/2504.15247")


@pytest.mark.parametrize("url", ["http://127.1/x.pdf", "http://2130706433/x.pdf", "http://0x7f000001/x.pdf"])
def test_alternative_spellings_of_loopback_are_refused(monkeypatch, url):
    assert is_public_http_url(url)
    monkeypatch.setattr(network, "_resolve", REAL_RESOLVE)
    with pytest.raises(UnsafeURLError):
        guard(url)


def test_ip_literals_are_not_resolved(monkeypatch):
    async def fail(host, port):
        raise AssertionError("resolved an IP literal")

    monkeypatch.setattr(network, "_resolve", fail)
    guard("https://8.8.8.8/paper.pdf")


def test_resolution_failure_is_left_to_the_connection(monkeypatch):
    async def fail(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

    monkeypatch.setattr(network, "_resolve", fail)
    guard("https://no-such-host.example/paper.pdf")


@pytest.mark.parametrize(
    "url",
    [
        "https://dl.acm.org/doi/pdf/10.1145/3802514.3812602",
        "http://www.cidrdb.org/cidr2023/papers/p92-jain.pdf",
        "https://8.8.8.8/paper.pdf",
        "HTTPS://Example.org/Paper.PDF",
    ],
)
def test_public_http_urls_are_allowed(url):
    assert is_public_http_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/paper.pdf",
        "data:application/pdf;base64,JVBERi0=",
        "javascript:alert(1)",
        "http://localhost/paper.pdf",
        "http://LOCALHOST./paper.pdf",
        "http://printer.local/paper.pdf",
        "http://app.localhost/paper.pdf",
        "http://127.0.0.1/paper.pdf",
        "http://[::1]/paper.pdf",
        "http://10.0.0.5/paper.pdf",
        "http://172.16.3.4/paper.pdf",
        "http://192.168.1.10/paper.pdf",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/paper.pdf",
        "http://224.0.0.1/paper.pdf",
        "https:///no-host",
        "not a url",
    ],
)
def test_local_and_non_http_urls_are_refused(url):
    assert not is_public_http_url(url)


def test_same_host_requests_are_spaced(sleeps):
    async def scenario():
        limiter = HostLimiter(1.5)
        await limiter.wait("arxiv.org")
        await limiter.wait("arxiv.org")

    asyncio.run(scenario())
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.5, abs=0.1)


def test_different_hosts_are_not_delayed(sleeps):
    async def scenario():
        limiter = HostLimiter(1.5)
        await limiter.wait("arxiv.org")
        await limiter.wait("api.openalex.org")

    asyncio.run(scenario())
    assert sleeps == []


def test_zero_interval_never_waits(sleeps):
    async def scenario():
        limiter = HostLimiter(0)
        for _ in range(3):
            await limiter.wait("arxiv.org")

    asyncio.run(scenario())
    assert sleeps == []
