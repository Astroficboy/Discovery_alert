"""Retries, backoff, and the containment rules for research fetches."""

from __future__ import annotations

import httpx
import pytest

from src.net import (
    FetchError,
    HttpClient,
    RetryPolicy,
    domain_matches,
    gather_resilient,
    is_allowed_url,
)


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "https://commons.wikimedia.org/w/api.php",
    "https://images-api.nasa.gov/search",
    "https://www.loc.gov/collections/x/",
    "https://folkways.si.edu/x",
])
def test_institutional_urls_are_allowed(url):
    allowed, reason = is_allowed_url(url, check_dns=False)
    assert allowed, reason


@pytest.mark.parametrize("url,fragment", [
    ("https://evil.example/x", "allowlist"),
    ("https://notnasa.gov/x", "allowlist"),
    ("file:///etc/passwd", "blocked scheme"),
    ("data:text/html,<script>", "blocked scheme"),
    ("javascript:alert(1)", "blocked scheme"),
    ("https://user:pass@www.loc.gov/x", "credentials"),
    ("ftp://archive.org/x", "blocked scheme"),
    ("not-a-url", "unsupported scheme"),
])
def test_disallowed_urls_are_refused(url, fragment):
    allowed, reason = is_allowed_url(url, check_dns=False)
    assert not allowed
    assert fragment in reason


def test_extra_domains_can_be_configured():
    assert not is_allowed_url("https://my-archive.example/x", check_dns=False)[0]
    assert is_allowed_url("https://my-archive.example/x",
                          extra_domains={"my-archive.example"}, check_dns=False)[0]


def test_domain_matching_is_on_label_boundaries():
    assert domain_matches("a.b.nasa.gov", {"nasa.gov"})
    assert domain_matches("nasa.gov", {"nasa.gov"})
    assert not domain_matches("evilnasa.gov", {"nasa.gov"})
    assert not domain_matches("nasa.gov.evil.com", {"nasa.gov"})


def test_private_addresses_are_refused():
    """SSRF: a research fetch must not be able to probe the local network."""
    # These hosts are not on the allowlist either, but the address check is the
    # backstop that matters if someone widens the allowlist carelessly.
    for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "[::1]"):
        allowed, _ = is_allowed_url(f"http://{host}/x", extra_domains={host.strip("[]")})
        assert not allowed


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #
def _client(handler, attempts: int = 3) -> HttpClient:
    client = HttpClient(transport=httpx.MockTransport(handler),
                        retry=RetryPolicy(attempts=attempts, base_delay=0.0, jitter=0.0))
    client._sleep = _no_sleep
    return client


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        assert await client.get_json("https://commons.wikimedia.org/x") == {"ok": True}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retries_are_bounded():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    async with _client(handler, attempts=3) as client:
        with pytest.raises(FetchError, match="giving up after 3 attempts"):
            await client.get_json("https://commons.wikimedia.org/x")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_a_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    async with _client(handler) as client:
        with pytest.raises(FetchError, match="HTTP 404"):
            await client.get_json("https://commons.wikimedia.org/x")
    assert calls["n"] == 1, "a 404 will not become a 200 by asking again"


@pytest.mark.asyncio
async def test_retry_after_is_honoured():
    seen: list[float] = []

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "7"}, json={})

    async def record(seconds: float) -> None:
        seen.append(seconds)

    client = _client(handler, attempts=2)
    client._sleep = record
    async with client:
        with pytest.raises(FetchError):
            await client.get_json("https://commons.wikimedia.org/x")
    assert seen == [7.0]


@pytest.mark.asyncio
async def test_timeouts_are_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("too slow")
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        assert await client.get_json("https://commons.wikimedia.org/x") == {"ok": True}


@pytest.mark.asyncio
async def test_non_json_response_is_an_error():
    async with _client(lambda r: httpx.Response(200, text="<html>")) as client:
        with pytest.raises(FetchError, match="not JSON"):
            await client.get_json("https://commons.wikimedia.org/x")


@pytest.mark.asyncio
async def test_head_falls_back_to_a_ranged_get():
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(405)
        assert request.headers.get("Range")
        return httpx.Response(206, headers={"Content-Type": "image/jpeg"})

    async with _client(handler) as client:
        response = await client.head_or_get("https://upload.wikimedia.org/x.jpg")
    assert response.headers["Content-Type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_gather_resilient_survives_one_bad_task():
    async def good():
        return "ok"

    async def bad():
        raise RuntimeError("archive is down")

    results = await gather_resilient([good(), bad(), good()], label="test")
    assert results == ["ok", "ok"]


@pytest.mark.asyncio
async def test_using_the_client_outside_its_context_is_an_error():
    client = HttpClient()
    with pytest.raises(RuntimeError, match="async context manager"):
        _ = client.client


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(attempts=6, base_delay=1.0, max_delay=10.0, jitter=0.0)
    delays = [policy.delay_for(attempt) for attempt in range(1, 7)]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert all(delay <= 10.0 for delay in delays)
    assert delays == sorted(delays)
