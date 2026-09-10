"""Europeana - the aggregated digital collections of European institutions.

The single best counterweight to an otherwise Anglo-American pipeline: Polish
archives, Dutch museums, Italian libraries, Finnish sound collections. The
``reusability=open`` filter does most of the licensing work for us, and every
returned rights statement is still passed through our own classifier.
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

SEARCH_URL = "https://api.europeana.eu/record/v2/search.json"

DEFAULT_QUERIES = (
    "musical instrument",
    "recording studio",
    "expedition photograph",
    "industrial machine",
    "observatory",
)


@register("europeana")
class Europeana(DiscoverySource):
    authority = 82
    requires_key = "europeana"

    async def fetch(self, limit: int) -> list[Candidate]:
        queries = list(self.options.get("queries") or DEFAULT_QUERIES)
        per_query = max(2, limit // max(len(queries), 1))
        tasks = [self._search(query, per_query) for query in queries]
        results = await gather_resilient(tasks, label="europeana query")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(SEARCH_URL, params={
            "wskey": self.api_key or "",
            "query": query,
            "media": "true",
            "thumbnail": "true",
            "reusability": "open",
            "qf": 'TYPE:"IMAGE"',
            "rows": str(min(limit * 2, 40)),
            "profile": "rich",
        })
        items = data.get("items") or []
        out: list[Candidate] = []
        for item in items:
            candidate = self._to_candidate(item, query)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, item: dict[str, Any], query: str) -> Candidate | None:
        url = _first(item.get("edmIsShownBy")) or _first(item.get("edmPreview"))
        if not url:
            return None
        title = clean_text(_first(item.get("title")) or "")
        description = clean_text(
            _first(item.get("dcDescription")) or _first(item.get("dcDescriptionLangAware")) or "",
            max_chars=1400,
        )
        rights = _first(item.get("rights")) or ""
        provider = clean_text(_first(item.get("dataProvider")) or "Europeana")
        creator = clean_text(_first(item.get("dcCreator")) or "") or None
        year = clean_text(_first(item.get("year")) or "") or None
        country = clean_text(_first(item.get("country")) or "") or None

        image = ImageAsset(
            url=url,
            page_url=item.get("guid") or (f"https://www.europeana.eu/item{item.get('id','')}"
                                          if item.get("id") else None),
            thumbnail_url=_first(item.get("edmPreview")),
            title=title,
            description=description or None,
            creator=creator,
            created=year,
            institution=provider,
            license=LicenseInfo(raw=rights),
        )
        blob = " ".join([title, description, query, country or ""])
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")

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
            date_hint=year,
            location_hint=country,
            raw={"europeana_query": query, "country": country},
        )


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    if isinstance(value, str):
        return value
    return None


__all__ = ["Europeana"]
