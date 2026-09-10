"""HTTP with retries, backoff and a domain allowlist.

Two jobs:

* **Resilience.** Archives go down, rate-limit, and time out. Every request
  retries with exponential backoff and jitter, honours ``Retry-After``, and
  gives up cleanly rather than hanging a scheduled run.
* **Containment.** The research stage follows links found in external
  documents. :func:`is_allowed_url` keeps it on the list of institutions we
  actually trust, and blocks private/loopback/link-local addresses so a
  redirect cannot turn the pipeline into an SSRF probe of whatever network it
  happens to be running on.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from .logging_setup import Stage, get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})


class FetchError(RuntimeError):
    """A request that could not be completed after all retries."""

    def __init__(self, url: str, reason: str, *, status: int | None = None) -> None:
        super().__init__(f"{reason} ({url})")
        self.url = url
        self.reason = reason
        self.status = status


class DisallowedURL(FetchError):
    """A URL that policy forbids fetching."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(url, reason)


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #
#: Institutions the research stage is allowed to read. Suffix-matched, so
#: "nasa.gov" covers "images-api.nasa.gov". Extend via
#: ``research.allowed_domains_extra`` in config.yaml.
ALLOWED_RESEARCH_DOMAINS: frozenset[str] = frozenset({
    # Reference and aggregation
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wikisource.org",
    "commons.wikimedia.org", "archive.org", "doi.org", "handle.net",
    # Space agencies
    "nasa.gov", "jpl.nasa.gov", "esa.int", "eso.org", "noirlab.edu",
    "spacetelescope.org", "webbtelescope.org", "hubblesite.org", "jaxa.jp",
    "isro.gov.in",
    # Science and environment
    "noaa.gov", "usgs.gov", "nih.gov", "nsf.gov", "cern", "cern.ch",
    "nature.com", "science.org", "pnas.org", "arxiv.org", "plos.org",
    "royalsociety.org", "ncbi.nlm.nih.gov", "usda.gov", "epa.gov",
    # Libraries, archives, museums
    "loc.gov", "archives.gov", "si.edu", "britishmuseum.org", "bl.uk",
    "nationalarchives.gov.uk", "europeana.eu", "metmuseum.org", "getty.edu",
    "rijksmuseum.nl", "nga.gov", "moma.org", "tate.org.uk", "vam.ac.uk",
    "nypl.org", "digitalcommonwealth.org", "iwm.org.uk", "rmg.co.uk",
    "sciencemuseum.org.uk", "nationalmuseum.ch", "smb.museum",
    "history.navy.mil", "af.mil", "army.mil", "nps.gov",
    # Universities and scholarly presses
    "edu", "ac.uk", "cam.ac.uk", "ox.ac.uk", "mit.edu", "stanford.edu",
    "harvard.edu", "jstor.org", "cambridge.org", "oup.com",
    # Music-specific archives and institutions
    "folkways.si.edu", "rockhall.com", "grammymuseum.org", "sfjazz.org",
    "discogs.com", "musicbrainz.org", "secondhandsongs.com",
    "bbc.co.uk", "npr.org", "pbs.org", "abc.net.au", "cbc.ca",
    "sangeetnatak.gov.in", "indianculture.gov.in", "archive.nptel.ac.in",
    # Reputable general publications used for cross-checking
    "smithsonianmag.com", "nationalgeographic.com", "scientificamerican.com",
    "newscientist.com", "theatlantic.com", "reuters.com", "apnews.com",
    "britannica.com", "atlasobscura.com",
})

_BLOCKED_SCHEMES = frozenset({"file", "ftp", "gopher", "data", "javascript"})


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def domain_matches(host: str, allowed: frozenset[str] | set[str]) -> bool:
    """Suffix match on label boundaries: ``a.nasa.gov`` matches ``nasa.gov``,
    ``notnasa.gov`` does not."""
    host = host.lower().rstrip(".")
    for entry in allowed:
        entry = entry.lower().strip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _is_private_address(host: str) -> bool:
    """Resolve and reject private, loopback, link-local and reserved space."""
    try:
        addr = ipaddress.ip_address(host)
        candidates = [addr]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return True  # cannot resolve -> refuse
        candidates = []
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:  # pragma: no cover - defensive
                return True
    return any(
        a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
        or a.is_multicast or a.is_unspecified
        for a in candidates
    )


def is_allowed_url(url: str, *, extra_domains: frozenset[str] | set[str] = frozenset(),
                   check_dns: bool = True) -> tuple[bool, str]:
    """Policy check for research fetches. Returns ``(allowed, reason)``."""
    parsed = urlparse(url)
    if parsed.scheme in _BLOCKED_SCHEMES:
        return False, f"blocked scheme: {parsed.scheme}"
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme: {parsed.scheme or '(none)'}"
    host = _hostname(url)
    if not host:
        return False, "no hostname"
    if "@" in (parsed.netloc or ""):
        return False, "credentials embedded in URL"
    allowed = ALLOWED_RESEARCH_DOMAINS | set(extra_domains)
    if not domain_matches(host, allowed):
        return False, f"host not on the research allowlist: {host}"
    if check_dns and _is_private_address(host):
        return False, f"host resolves to a private or reserved address: {host}"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.3

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw * (1 + random.uniform(-self.jitter, self.jitter))  # noqa: S311


@dataclass
class HttpClient:
    """Thin async wrapper over httpx with the project's retry semantics."""

    user_agent: str = "CuriousThings/1.0"
    timeout: float = 20.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    _sleep: Any = field(default=None, repr=False)

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
                "Accept-Language": "en",
            },
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            transport=self.transport,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient must be used as an async context manager")
        return self._client

    async def _pause(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: str = "unknown error"
        status: int | None = None
        for attempt in range(1, self.retry.attempts + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("request failed", url=url, attempt=attempt, error=last_error,
                             stage=Stage.DISCOVERY)
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    if response.status_code >= 400:
                        raise FetchError(url, f"HTTP {response.status_code}",
                                         status=response.status_code)
                    return response
                status = response.status_code
                last_error = f"HTTP {response.status_code}"
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if attempt < self.retry.attempts:
                    await self._pause(self.retry.delay_for(attempt, retry_after))
                continue
            if attempt < self.retry.attempts:
                await self._pause(self.retry.delay_for(attempt))
        raise FetchError(url, f"giving up after {self.retry.attempts} attempts: {last_error}",
                         status=status)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(url, f"response was not JSON: {exc}") from exc

    async def get_text(self, url: str, *, max_bytes: int = 2_000_000, **kwargs: Any) -> str:
        response = await self.request("GET", url, **kwargs)
        content = response.content[:max_bytes]
        encoding = response.encoding or "utf-8"
        return content.decode(encoding, errors="replace")

    async def head_or_get(self, url: str, **kwargs: Any) -> httpx.Response:
        """HEAD where supported, falling back to a ranged GET. Used to confirm
        an image exists and to read its size/type without downloading it."""
        try:
            return await self.request("HEAD", url, **kwargs)
        except FetchError:
            headers = {**kwargs.pop("headers", {}), "Range": "bytes=0-2047"}
            return await self.request("GET", url, headers=headers, **kwargs)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; the exponential backoff covers it


async def gather_resilient(tasks: list[Any], *, label: str = "task") -> list[Any]:
    """``asyncio.gather`` that logs failures instead of aborting the batch.

    A dead archive should cost us its candidates, not the edition.
    """
    results = await asyncio.gather(*tasks, return_exceptions=True)
    output = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(f"{label} failed", error=f"{type(result).__name__}: {result}")
            continue
        output.append(result)
    return output


__all__ = [
    "ALLOWED_RESEARCH_DOMAINS",
    "DisallowedURL",
    "FetchError",
    "HttpClient",
    "RetryPolicy",
    "domain_matches",
    "gather_resilient",
    "is_allowed_url",
]
