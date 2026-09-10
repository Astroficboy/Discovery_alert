"""NASA: Astronomy Picture of the Day, and the image library.

A licensing subtlety worth being explicit about, because getting it wrong is
the easiest way to publish someone else's copyrighted photograph:

**NASA imagery is generally public domain, but APOD is not a NASA image
feed.** APOD publishes work by amateur and professional astrophotographers who
retain copyright; the API marks those with a ``copyright`` field. This source
therefore accepts an APOD entry **only when that field is absent**, and even
then only when the entry is an image rather than a video. The same caution
applies to the image library, which contains partner material.
"""

from __future__ import annotations

from typing import Any

from ..editorial.taxonomy import classify_domains, entities_from_text, keywords_from_text
from ..logging_setup import get_logger
from ..models import Candidate, ImageAsset, LicenseInfo
from ..net import gather_resilient
from ..sanitize import clean_text
from .base import DiscoverySource, register

logger = get_logger(__name__)

APOD_URL = "https://api.nasa.gov/planetary/apod"
IMAGES_SEARCH = "https://images-api.nasa.gov/search"
IMAGES_ASSET = "https://images-api.nasa.gov/asset/{nasa_id}"


@register("nasa_apod")
class NasaApod(DiscoverySource):
    authority = 88
    requires_key = "nasa"

    def check_available(self) -> None:
        # DEMO_KEY works, just slowly. Absence of a key is not fatal here.
        if not self.api_key:
            self.config.source_api_keys.setdefault("nasa", "DEMO_KEY")

    async def fetch(self, limit: int) -> list[Candidate]:
        data = await self.http.get_json(APOD_URL, params={
            "api_key": self.api_key or "DEMO_KEY",
            "count": str(min(limit * 2, 40)),
            "thumbs": "true",
        })
        entries = data if isinstance(data, list) else [data]
        out: list[Candidate] = []
        skipped_copyright = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("media_type") != "image":
                continue
            if entry.get("copyright"):
                # Photographer retains copyright. Interesting, but not ours.
                skipped_copyright += 1
                continue
            url = entry.get("hdurl") or entry.get("url")
            if not url:
                continue
            title = clean_text(entry.get("title", ""))
            explanation = clean_text(entry.get("explanation", ""), max_chars=1500)
            image = ImageAsset(
                url=url,
                page_url=f"https://apod.nasa.gov/apod/ap{_apod_slug(entry.get('date',''))}.html",
                thumbnail_url=entry.get("url"),
                title=title,
                description=explanation[:1200] or None,
                created=entry.get("date"),
                creator="NASA",
                institution="NASA Astronomy Picture of the Day",
                license=LicenseInfo(raw="NASA image use policy: public domain"),
            )
            blob = f"{title} {explanation}"
            out.append(Candidate(
                source=self.name,
                source_url=image.page_url or url,
                title=title,
                description=explanation,
                image=image,
                categories=sorted(set(["space", *classify_domains(
                    blob, allowed=self.config.content.categories)])),
                keywords=keywords_from_text(blob),
                entities=entities_from_text(title, explanation),
                date_hint=entry.get("date"),
                raw={"apod_date": entry.get("date")},
            ))
            if len(out) >= limit:
                break
        if skipped_copyright:
            logger.debug("APOD entries skipped as photographer-copyrighted",
                         source=self.name, count=skipped_copyright)
        return out


def _apod_slug(date_string: str) -> str:
    """``2026-09-10`` -> ``260910``, the APOD permalink form."""
    parts = date_string.split("-")
    if len(parts) != 3:
        return ""
    return f"{parts[0][2:]}{parts[1]}{parts[2]}"


@register("nasa_images")
class NasaImageLibrary(DiscoverySource):
    """images.nasa.gov - mission photography, hardware, test facilities.

    This is where the *engineering* stories live: wind tunnels, mission
    control, technicians standing next to something enormous.
    """

    authority = 85
    DEFAULT_QUERIES = ("apollo", "wind tunnel", "mission control", "telescope assembly")

    async def fetch(self, limit: int) -> list[Candidate]:
        queries = list(self.options.get("queries") or self.DEFAULT_QUERIES)
        per_query = max(2, limit // max(len(queries), 1) + 1)
        tasks = [self._search(query, per_query) for query in queries]
        results = await gather_resilient(tasks, label="nasa image search")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _search(self, query: str, limit: int) -> list[Candidate]:
        data = await self.http.get_json(IMAGES_SEARCH, params={
            "q": query, "media_type": "image", "page_size": str(min(limit * 2, 50)),
        })
        items = (data.get("collection") or {}).get("items") or []
        out: list[Candidate] = []
        for item in items:
            candidate = self._to_candidate(item, query)
            if candidate:
                out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def _to_candidate(self, item: dict[str, Any], query: str) -> Candidate | None:
        records = item.get("data") or []
        links = item.get("links") or []
        if not records or not links:
            return None
        record = records[0]
        preview = next((link.get("href") for link in links if link.get("render") == "image"), None)
        preview = preview or links[0].get("href")
        if not preview:
            return None

        rights = clean_text(str(record.get("rights") or ""))
        secondary = clean_text(str(record.get("secondary_creator") or ""))
        if rights and "public domain" not in rights.lower():
            # Partner-supplied material with its own terms.
            return None

        title = clean_text(record.get("title", ""))
        description = clean_text(record.get("description", ""), max_chars=1500)
        center = record.get("center") or "NASA"
        nasa_id = record.get("nasa_id", "")

        image = ImageAsset(
            url=preview,
            page_url=f"https://images.nasa.gov/details-{nasa_id}" if nasa_id else None,
            thumbnail_url=preview,
            title=title,
            description=description[:1200] or None,
            creator=secondary or f"NASA/{center}",
            created=clean_text(str(record.get("date_created") or ""))[:10] or None,
            institution=f"NASA ({center})",
            license=LicenseInfo(raw="NASA image use policy: public domain"),
        )
        blob = " ".join([title, description, " ".join(record.get("keywords") or [])])
        domains = classify_domains(blob, allowed=self.config.content.categories)
        return Candidate(
            source=self.name,
            source_url=image.page_url or preview,
            title=title,
            description=description,
            image=image,
            categories=sorted(set(["space", *domains])),
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title, description),
            date_hint=image.created,
            raw={"nasa_id": nasa_id, "query": query},
        )


__all__ = ["NasaApod", "NasaImageLibrary"]
