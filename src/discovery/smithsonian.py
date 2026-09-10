"""Smithsonian Open Access.

Nineteen museums, the National Air and Space Museum among them, plus
Smithsonian Folkways - one of the most important recorded-music archives in
the world - all behind one API. Requires a free api.data.gov key.

Only records the Smithsonian itself marks ``CC0`` are accepted.
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

SEARCH_URL = "https://api.si.edu/openaccess/api/v1.0/search"

DEFAULT_QUERIES = (
    'online_media_type:"Images" AND topic:"Music"',
    'online_media_type:"Images" AND unit_code:"NASM"',
    'online_media_type:"Images" AND topic:"Photography"',
    'online_media_type:"Images" AND topic:"Science"',
)


@register("smithsonian")
class Smithsonian(DiscoverySource):
    authority = 88
    requires_key = "smithsonian"

    async def fetch(self, limit: int) -> list[Candidate]:
        queries = list(self.options.get("queries") or DEFAULT_QUERIES)
        per_query = max(2, limit // max(len(queries), 1))
        tasks = [self._search(query, per_query) for query in queries]
        results = await gather_resilient(tasks, label="smithsonian query")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(SEARCH_URL, params={
            "api_key": self.api_key or "",
            "q": query,
            "rows": str(min(limit * 3, 60)),
        })
        rows = ((data.get("response") or {}).get("rows")) or []
        out: list[Candidate] = []
        for row in rows:
            candidate = self._to_candidate(row)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, row: dict[str, Any]) -> Candidate | None:
        content = row.get("content") or {}
        descriptive = content.get("descriptiveNonRepeating") or {}
        media_block = descriptive.get("online_media") or {}
        media_items = media_block.get("media") or []
        media = next(
            (m for m in media_items
             if isinstance(m, dict) and (m.get("usage") or {}).get("access") == "CC0"),
            None,
        )
        if not media:
            return None
        url = media.get("content") or media.get("thumbnail")
        if not url:
            return None

        title = clean_text(row.get("title", ""))
        freetext = content.get("freetext") or {}
        notes = _freetext(freetext, "notes")
        physical = _freetext(freetext, "physicalDescription")
        topics = _indexed(content, "topic")
        place = _indexed(content, "place")
        names = _indexed(content, "name")
        date = _indexed(content, "date")

        description = clean_text(" · ".join(filter(None, [notes, physical])), max_chars=1400)
        unit = clean_text(str(descriptive.get("unit_code") or "Smithsonian Institution"))

        image = ImageAsset(
            url=url,
            page_url=descriptive.get("record_link") or descriptive.get("guid"),
            thumbnail_url=media.get("thumbnail") or url,
            title=title,
            description=description or None,
            creator=clean_text(names[0]) if names else None,
            created=clean_text(date[0]) if date else None,
            institution=f"Smithsonian Institution ({unit})",
            license=LicenseInfo(raw="CC0"),
        )
        blob = " ".join([title, description, " ".join(topics), " ".join(names)])
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
            entities=entities_from_text(title, description) + names[:4],
            date_hint=image.created,
            location_hint=clean_text(place[0]) if place else None,
            raw={"si_unit": unit, "topics": topics[:12]},
        )


def _freetext(freetext: dict[str, Any], key: str) -> str:
    entries = freetext.get(key) or []
    return " ".join(
        str(entry.get("content", "")) for entry in entries if isinstance(entry, dict)
    )


def _indexed(content: dict[str, Any], key: str) -> list[str]:
    indexed = content.get("indexedStructured") or {}
    values = indexed.get(key) or []
    out: list[str] = []
    for value in values:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict) and value.get("content"):
            out.append(str(value["content"]))
    return out


__all__ = ["Smithsonian"]
