"""Two more keyless museum APIs.

Both exist for the same reason as :mod:`src.discovery.openverse`: they need no
API key, so they keep working when a keyed source cannot run. Between them
they add a few hundred thousand objects with unambiguous rights status - the
Art Institute marks `is_public_domain`, Cleveland marks `share_license_status`
- which is exactly the kind of metadata the copyright gate needs and a raw
web scrape cannot supply.
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

AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
AIC_IIIF_FALLBACK = "https://www.artic.edu/iiif/2"
CMA_SEARCH = "https://openaccess-api.clevelandart.org/api/artworks/"


@register("art_institute")
class ArtInstituteOfChicago(DiscoverySource):
    """api.artic.edu - keyless, and generous with high-resolution IIIF images."""

    authority = 84
    fallback = True

    DEFAULT_QUERIES = ("photograph", "scientific instrument", "textile", "armor",
                       "musical instrument")

    FIELDS = ("id,title,image_id,is_public_domain,artist_display,date_display,"
              "medium_display,classification_title,place_of_origin,thumbnail,"
              "term_titles,department_title")

    async def fetch(self, limit: int) -> list[Candidate]:
        queries = list(self.options.get("queries") or self.DEFAULT_QUERIES)
        per_query = max(2, limit // max(len(queries), 1) + 1)
        results = await gather_resilient(
            [self._search(query, per_query) for query in queries], label="aic query"
        )
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(AIC_SEARCH, params={
            "q": query, "limit": str(min(limit * 3, 60)), "fields": self.FIELDS,
        })
        iiif = (data.get("config") or {}).get("iiif_url") or AIC_IIIF_FALLBACK
        out: list[Candidate] = []
        for item in data.get("data") or []:
            candidate = self._to_candidate(item, iiif, query)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, item: dict[str, Any], iiif: str, query: str) -> Candidate | None:
        if not item.get("is_public_domain"):
            return None
        image_id = item.get("image_id")
        if not image_id:
            return None

        title = clean_text(str(item.get("title") or ""))
        thumbnail = item.get("thumbnail") or {}
        terms = [str(t) for t in (item.get("term_titles") or [])]
        pieces = [item.get("artist_display"), item.get("date_display"),
                  item.get("medium_display"), item.get("place_of_origin"),
                  thumbnail.get("alt_text")]
        description = clean_text(" · ".join(str(p) for p in pieces if p), max_chars=1200)

        image = ImageAsset(
            url=f"{iiif.rstrip('/')}/{image_id}/full/1686,/0/default.jpg",
            page_url=f"https://www.artic.edu/artworks/{item.get('id')}",
            thumbnail_url=f"{iiif.rstrip('/')}/{image_id}/full/843,/0/default.jpg",
            width=thumbnail.get("width"),
            height=thumbnail.get("height"),
            mime_type="image/jpeg",
            title=title or None,
            description=description or None,
            creator=clean_text(str(item.get("artist_display") or "")) or None,
            created=clean_text(str(item.get("date_display") or ""))[:60] or None,
            institution="Art Institute of Chicago",
            license=LicenseInfo(raw="CC0 (Art Institute of Chicago, public domain)"),
        )
        blob = " ".join([title, description, " ".join(terms), query])
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")
        if "culture" not in domains:
            domains.append("culture")

        return Candidate(
            source=self.name,
            source_url=image.page_url or image.url,
            title=title,
            description=description,
            image=image,
            categories=sorted(set(domains)),
            music=music,
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title, description),
            date_hint=image.created,
            location_hint=clean_text(str(item.get("place_of_origin") or "")) or None,
            raw={"aic_id": item.get("id"), "query": query},
        )


@register("cleveland_museum")
class ClevelandMuseumOfArt(DiscoverySource):
    """openaccess-api.clevelandart.org - keyless, CC0-filtered at the API."""

    authority = 84
    fallback = True

    DEFAULT_QUERIES = ("photograph", "instrument", "armor", "manuscript", "print")

    async def fetch(self, limit: int) -> list[Candidate]:
        queries = list(self.options.get("queries") or self.DEFAULT_QUERIES)
        per_query = max(2, limit // max(len(queries), 1) + 1)
        results = await gather_resilient(
            [self._search(query, per_query) for query in queries], label="cma query"
        )
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(CMA_SEARCH, params={
            "q": query, "has_image": "1", "cc0": "1",
            "limit": str(min(limit * 2, 50)),
        })
        out: list[Candidate] = []
        for item in data.get("data") or []:
            candidate = self._to_candidate(item, query)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, item: dict[str, Any], query: str) -> Candidate | None:
        if str(item.get("share_license_status") or "").upper() != "CC0":
            return None
        images = item.get("images") or {}
        rendering = images.get("web") or images.get("print") or images.get("full") or {}
        url = rendering.get("url")
        if not url:
            return None

        title = clean_text(str(item.get("title") or ""))
        creators = item.get("creators") or []
        creator = clean_text(str(creators[0].get("description", ""))) if creators else None
        culture = item.get("culture") or []
        pieces = [item.get("creation_date"), item.get("technique"),
                  ", ".join(str(c) for c in culture), item.get("department")]
        description = clean_text(" · ".join(str(p) for p in pieces if p), max_chars=1200)
        if extra := clean_text(str(item.get("description") or "")):
            description = clean_text(f"{description} · {extra}", max_chars=1400)

        image = ImageAsset(
            url=url,
            page_url=item.get("url"),
            thumbnail_url=(images.get("web") or {}).get("url") or url,
            width=_as_int(rendering.get("width")),
            height=_as_int(rendering.get("height")),
            mime_type="image/jpeg",
            title=title or None,
            description=description or None,
            creator=creator,
            created=clean_text(str(item.get("creation_date") or ""))[:60] or None,
            institution="Cleveland Museum of Art",
            license=LicenseInfo(raw="CC0"),
        )
        blob = " ".join([title, description, query])
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")
        if "culture" not in domains:
            domains.append("culture")

        return Candidate(
            source=self.name,
            source_url=image.page_url or url,
            title=title,
            description=description,
            image=image,
            categories=sorted(set(domains)),
            music=music,
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title, description),
            date_hint=image.created,
            raw={"cma_id": item.get("id"), "query": query},
        )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["ArtInstituteOfChicago", "ClevelandMuseumOfArt"]
