"""How much a source is worth.

Authority is a 0-100 number attached to every reference, and it does real
work: it gates which sources may support a load-bearing claim, it feeds the
``source_quality`` scoring dimension, and it decides the order of the
"Sources & Further Reading" list.

The scale, roughly:

    95  the institution that holds the object, or the agency that did the thing
    85  national archives, major museums, peer-reviewed literature
    75  universities, public broadcasters, established science press
    65  reference works and quality general publications
    50  Wikipedia and comparable aggregations - fine for orientation and for
        finding primary sources, never sufficient alone for a key claim
    30  anything else that got through the allowlist

Fan sites, forums and content farms do not appear because they never clear
the network allowlist in :mod:`src.net`. They can still be useful leads; they
are simply not evidence.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..models import SourceRef

#: Exact-ish domain authority. Suffix matched, longest match wins.
AUTHORITY: dict[str, int] = {
    # Primary institutional holders
    "nasa.gov": 95, "jpl.nasa.gov": 95, "esa.int": 94, "noaa.gov": 93,
    "usgs.gov": 92, "history.navy.mil": 93, "nps.gov": 88, "archives.gov": 95,
    "loc.gov": 95, "si.edu": 94, "folkways.si.edu": 94, "bl.uk": 93,
    "nationalarchives.gov.uk": 94, "britishmuseum.org": 92, "metmuseum.org": 92,
    "rijksmuseum.nl": 91, "getty.edu": 91, "vam.ac.uk": 90, "iwm.org.uk": 91,
    "rmg.co.uk": 90, "sciencemuseum.org.uk": 90, "nypl.org": 88,
    "europeana.eu": 85, "tate.org.uk": 89, "nga.gov": 89, "moma.org": 87,
    "sangeetnatak.gov.in": 88, "indianculture.gov.in": 86, "isro.gov.in": 92,
    "rockhall.com": 78, "grammymuseum.org": 75,
    # Peer-reviewed and scholarly
    "nature.com": 93, "science.org": 93, "pnas.org": 92, "royalsociety.org": 91,
    "plos.org": 87, "ncbi.nlm.nih.gov": 90, "nih.gov": 90, "cambridge.org": 88,
    "oup.com": 88, "jstor.org": 86, "doi.org": 85, "arxiv.org": 72,
    # Universities
    "edu": 78, "ac.uk": 78, "cern.ch": 92, "eso.org": 91, "noirlab.edu": 90,
    # Public broadcasters and established press
    "bbc.co.uk": 76, "npr.org": 74, "pbs.org": 74, "cbc.ca": 73, "abc.net.au": 73,
    "reuters.com": 78, "apnews.com": 78,
    "smithsonianmag.com": 76, "nationalgeographic.com": 74,
    "scientificamerican.com": 76, "newscientist.com": 72, "theatlantic.com": 68,
    "britannica.com": 70, "atlasobscura.com": 58,
    # Reference and aggregation
    "wikipedia.org": 50, "wikimedia.org": 55, "commons.wikimedia.org": 58,
    "wikidata.org": 55, "wikisource.org": 62, "archive.org": 66,
    "musicbrainz.org": 58, "discogs.com": 48, "secondhandsongs.com": 45,
}

DEFAULT_AUTHORITY = 35

#: An authority at or above this counts as authoritative for a key claim.
AUTHORITATIVE_THRESHOLD = 70

_KIND_BY_PATTERN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\.(gov|mil)(/|$)"), "government"),
    (re.compile(r"\.edu(/|$)|ac\.uk(/|$)"), "academic"),
    (re.compile(r"doi\.org|arxiv\.org|ncbi|pnas|nature\.com|science\.org"), "scholarly"),
    (re.compile(r"museum|metmuseum|rijksmuseum|si\.edu|britishmuseum|tate|getty"), "museum"),
    (re.compile(r"loc\.gov|bl\.uk|archives|library|nypl"), "archive"),
    (re.compile(r"wikipedia|wikimedia|wikidata"), "reference"),
)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def authority_for(url: str) -> int:
    """Authority score for a URL, by longest matching domain suffix."""
    host = _host(url)
    if not host:
        return DEFAULT_AUTHORITY
    best_score = DEFAULT_AUTHORITY
    best_length = -1
    for domain, score in AUTHORITY.items():
        if (host == domain or host.endswith("." + domain)) and len(domain) > best_length:
            best_score, best_length = score, len(domain)
    return best_score


def kind_for(url: str) -> str:
    for pattern, kind in _KIND_BY_PATTERN:
        if pattern.search(url.lower()):
            return kind
    return "web"


def publisher_for(url: str) -> str:
    """A human-readable publisher name from the host."""
    host = _host(url)
    if not host:
        return "Unknown"
    host = host.removeprefix("www.")
    known = {
        "commons.wikimedia.org": "Wikimedia Commons",
        "en.wikipedia.org": "Wikipedia",
        "loc.gov": "Library of Congress",
        "si.edu": "Smithsonian Institution",
        "nasa.gov": "NASA",
        "archives.gov": "U.S. National Archives",
        "nationalarchives.gov.uk": "The National Archives (UK)",
        "bl.uk": "British Library",
        "metmuseum.org": "The Metropolitan Museum of Art",
        "europeana.eu": "Europeana",
        "nature.com": "Nature",
        "science.org": "Science",
        "bbc.co.uk": "BBC",
        "folkways.si.edu": "Smithsonian Folkways",
        "atlasobscura.com": "Atlas Obscura",
        "smithsonianmag.com": "Smithsonian Magazine",
        "nationalgeographic.com": "National Geographic",
        "scientificamerican.com": "Scientific American",
        "ox.ac.uk": "University of Oxford",
        "cam.ac.uk": "University of Cambridge",
        "mit.edu": "MIT",
        "archive.org": "Internet Archive",
        "musicbrainz.org": "MusicBrainz",
        "rockhall.com": "Rock & Roll Hall of Fame",
        "history.navy.mil": "U.S. Naval History and Heritage Command",
        "iwm.org.uk": "Imperial War Museums",
        "rmg.co.uk": "Royal Museums Greenwich",
    }
    if host in known:
        return known[host]
    for domain, name in known.items():
        if host.endswith("." + domain):
            return name
    parts = host.split(".")
    # For two-part public suffixes (ac.uk, co.uk, gov.au) the organisation is
    # the label before them; otherwise it is the second-level domain.
    if len(parts) > 2 and parts[-2] in ("co", "ac", "gov", "org", "net", "edu"):
        core = parts[-3]
    else:
        core = parts[-2] if len(parts) > 1 else parts[0]
    return core.replace("-", " ").title()


def make_source_ref(url: str, title: str, *, excerpt: str | None = None) -> SourceRef:
    return SourceRef(
        title=title.strip() or publisher_for(url),
        url=url,
        publisher=publisher_for(url),
        kind=kind_for(url),
        authority=authority_for(url),
        excerpt=excerpt,
    )


def rank_sources(sources: list[SourceRef]) -> list[SourceRef]:
    """Highest authority first, de-duplicated by URL, then by publisher.

    De-duplicating by publisher matters for the *appearance* of corroboration:
    four pages from the same museum are one source, not four.
    """
    seen_urls: set[str] = set()
    ordered = sorted(sources, key=lambda s: (-s.authority, s.publisher or "", s.title))
    out: list[SourceRef] = []
    for source in ordered:
        key = source.url.split("#")[0].rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        out.append(source)
    return out


def independent_publishers(urls: list[str]) -> int:
    return len({publisher_for(url) for url in urls if url})


__all__ = [
    "AUTHORITATIVE_THRESHOLD",
    "AUTHORITY",
    "authority_for",
    "independent_publishers",
    "kind_for",
    "make_source_ref",
    "publisher_for",
    "rank_sources",
]
