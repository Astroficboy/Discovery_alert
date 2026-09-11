"""Openverse - keyless search across openly-licensed images.

This is the project's answer to "search the web for a picture". Openverse
indexes roughly a hundred providers - Flickr Commons, museum collections,
Wikimedia, government archives - and, crucially, every result carries an
explicit licence field. That is what makes it usable here when a raw web
scrape is not: the copyright gate needs a licence it can positively identify,
and a scraped page almost never supplies one.

It needs no API key, which is the point. When a keyed source cannot run,
:mod:`src.discovery` hands its quota to this source instead, so a missing
`SMITHSONIAN_API_KEY` costs breadth rather than editions.

Its material is more variable than a national archive's, so it carries a
lower `authority` and its candidates are scored accordingly. The prefilter's
resolution floor and caption requirements do the rest.
"""

from __future__ import annotations

from typing import Any

from ..editorial.taxonomy import (
    classify_domains,
    detect_music,
    entities_from_text,
    keywords_from_text,
)
from ..logging_setup import get_logger
from ..models import Candidate, ImageAsset, LicenseInfo
from ..net import gather_resilient
from ..sanitize import clean_text
from .base import DiscoverySource, register

logger = get_logger(__name__)

SEARCH_URL = "https://api.openverse.org/v1/images/"

#: Only licences our gate accepts. Asking the API to filter saves us fetching
#: thousands of records we would immediately drop.
ALLOWED_LICENCES = "pdm,cc0,by,by-sa"

#: Openverse licence codes -> a string src.licensing can classify.
_LICENCE_NAMES = {
    "pdm": "Public domain mark",
    "cc0": "CC0",
    "by": "CC BY",
    "by-sa": "CC BY-SA",
}

DEFAULT_QUERIES = (
    "abandoned factory interior",
    "scientific instrument nineteenth century",
    "expedition photograph",
    "vintage recording studio",
    "traditional musical instrument",
    "industrial machinery archive",
    "observatory telescope historic",
    "shipwreck",
)


@register("openverse")
class Openverse(DiscoverySource):
    #: Deliberately below the institutional archives: the index is broad, and
    #: breadth is not the same as provenance.
    authority = 62
    #: Marks this source as one that absorbs a keyed source's quota when that
    #: source cannot run. See src/discovery/__init__.py.
    fallback = True

    async def fetch(self, limit: int) -> list[Candidate]:
        queries = list(self.options.get("queries") or DEFAULT_QUERIES)
        per_query = max(2, limit // max(len(queries), 1) + 1)
        tasks = [self._search(query, per_query) for query in queries]
        results = await gather_resilient(tasks, label="openverse query")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(SEARCH_URL, params={
            "q": query,
            "license": ALLOWED_LICENCES,
            "size": "large",
            "mature": "false",
            "page_size": str(min(limit * 3, 40)),
        })
        results = data.get("results") or []
        out: list[Candidate] = []
        for item in results:
            candidate = self._to_candidate(item, query)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, item: dict[str, Any], query: str) -> Candidate | None:
        url = item.get("url")
        if not url:
            return None

        licence_code = str(item.get("license") or "").lower()
        licence_name = _LICENCE_NAMES.get(licence_code)
        if licence_name is None:
            # An unrecognised code would be refused by the gate anyway; drop it
            # here so it never reaches the funnel.
            return None
        version = str(item.get("license_version") or "").strip()
        licence_raw = f"{licence_name} {version}".strip() if licence_code in ("by", "by-sa") \
            else licence_name

        title = clean_text(str(item.get("title") or ""))
        tags = [
            str(tag.get("name", "")) for tag in (item.get("tags") or [])
            if isinstance(tag, dict)
        ]
        provider = clean_text(str(item.get("source") or item.get("provider") or "Openverse"))
        creator = clean_text(str(item.get("creator") or "")) or None
        description = clean_text(" · ".join(filter(None, [
            title, ", ".join(tags[:12]), f"via {provider}",
        ])), max_chars=900)

        image = ImageAsset(
            url=url,
            page_url=item.get("foreign_landing_url") or item.get("detail_url"),
            thumbnail_url=item.get("thumbnail") or url,
            width=item.get("width"),
            height=item.get("height"),
            mime_type=_mime_for(str(item.get("filetype") or "")),
            title=title or None,
            description=description or None,
            creator=creator,
            institution=provider,
            license=LicenseInfo(raw=licence_raw, url=item.get("license_url")),
        )
        blob = " ".join([title, " ".join(tags), query])
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")

        return Candidate(
            source=self.name,
            source_url=image.page_url or url,
            title=title or query.title(),
            description=description,
            image=image,
            categories=sorted(set(domains)),
            music=music,
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title),
            raw={"openverse_query": query, "provider": provider,
                 "openverse_id": item.get("id")},
        )


def _mime_for(filetype: str) -> str | None:
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
    }.get(filetype.lower().lstrip("."))


__all__ = ["Openverse"]
