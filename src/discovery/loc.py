"""Library of Congress.

The LoC's ``?fo=json`` interface exposes their whole digital collection, and
several of those collections are among the best photographic archives that
exist: the FSA/OWI colour negatives, the Civil War glass plates, the
panoramic photographs.

Rights handling here is conservative and explicit. The LoC records rights as
prose, per item and per collection, and that prose is not always machine
readable. Rather than guess, this source:

* reads every rights field the item exposes and passes the lot to the licence
  classifier; and
* lets you assert a collection-level rights statement in config, for
  collections whose status is a documented fact (the FSA/OWI photographs, for
  instance, carry "No known restrictions on publication").

Anything still unclassified is dropped.
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

COLLECTION_URL = "https://www.loc.gov/collections/{slug}/"
SEARCH_URL = "https://www.loc.gov/photos/"

#: Collections whose rights status is documented by the Library itself.
#: Sourced from each collection's "Rights and Restrictions" page.
KNOWN_COLLECTION_RIGHTS: dict[str, str] = {
    "fsa-owi-photos": "No known restrictions on publication",
    "fsa-owi-black-and-white-negatives": "No known restrictions on publication",
    "civil-war-glass-negatives": "No known restrictions on publication",
    "panoramic-photographs": "No known restrictions on publication",
    "national-photo-company": "No known restrictions on publication",
    "harris-and-ewing": "No known restrictions on publication",
    "bain": "No known restrictions on publication",
}


@register("loc")
class LibraryOfCongress(DiscoverySource):
    authority = 90

    async def fetch(self, limit: int) -> list[Candidate]:
        collections = list(self.options.get("collections") or ["fsa-owi-photos"])
        queries = list(self.options.get("queries") or [])
        tasks = [
            self._fetch_collection(slug, max(3, limit // max(len(collections), 1)))
            for slug in collections
        ]
        tasks += [self._fetch_search(query, max(3, limit // 4)) for query in queries]
        results = await gather_resilient(tasks, label="loc query")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _fetch_collection(self, slug: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(COLLECTION_URL.format(slug=slug), params={
            "fo": "json", "c": str(min(limit * 2, 40)), "at": "results",
            "sb": "shuffle",
        })
        rights_default = self._configured_rights(slug)
        return self._parse(data, limit, rights_default=rights_default, origin=slug)

    async def _fetch_search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(SEARCH_URL, params={
            "q": query, "fo": "json", "c": str(min(limit * 2, 40)), "at": "results",
        })
        return self._parse(data, limit, rights_default=None, origin=f"search:{query}")

    def _configured_rights(self, slug: str) -> str | None:
        override = (self.options.get("collection_rights") or {}).get(slug)
        return override or KNOWN_COLLECTION_RIGHTS.get(slug)

    def _parse(self, data: Any, limit: int, *, rights_default: str | None,
               origin: str) -> list[Candidate]:
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        out: list[Candidate] = []
        for result in results:
            candidate = self._to_candidate(result, rights_default=rights_default, origin=origin)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, result: dict[str, Any], *, rights_default: str | None,
                      origin: str) -> Candidate | None:
        image_urls = result.get("image_url") or []
        if isinstance(image_urls, str):
            image_urls = [image_urls]
        image_urls = [u for u in image_urls if isinstance(u, str) and u.startswith("http")]
        if not image_urls:
            return None
        # The LoC lists renderings smallest-first; take the largest.
        url = image_urls[-1]

        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        rights_bits = [
            result.get("rights"),
            item.get("rights"),
            item.get("rights_advisory"),
            result.get("rights_advisory"),
            item.get("rights_information"),
        ]
        rights_text = " ".join(
            " ".join(bit) if isinstance(bit, list) else str(bit)
            for bit in rights_bits if bit
        ).strip()
        if not rights_text and rights_default:
            rights_text = rights_default

        title = clean_text(str(result.get("title") or item.get("title") or ""))
        description = clean_text(
            " ".join(result.get("description") or []) if isinstance(result.get("description"), list)
            else str(result.get("description") or ""),
            max_chars=1500,
        )
        notes = item.get("notes") or []
        if isinstance(notes, list) and notes:
            description = clean_text(f"{description} {' '.join(str(n) for n in notes)}",
                                     max_chars=1600)
        creators = item.get("creator") or result.get("contributor") or []
        creator = clean_text(", ".join(creators) if isinstance(creators, list) else str(creators))
        created = clean_text(str(result.get("date") or item.get("date") or ""))[:40]

        image = ImageAsset(
            url=url,
            page_url=result.get("id") or result.get("url"),
            thumbnail_url=image_urls[0],
            title=title,
            description=description[:1200] or None,
            creator=creator or None,
            created=created or None,
            institution="Library of Congress",
            license=LicenseInfo(raw=rights_text),
        )
        blob = f"{title} {description} {origin}"
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")
        if "photography" not in domains:
            domains.append("photography")

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
            date_hint=created or None,
            raw={"loc_origin": origin},
        )


__all__ = ["LibraryOfCongress"]
