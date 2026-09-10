"""Wikipedia's daily featured feed.

The REST feed hands back the day's featured article, the picture of the day
and "on this day" events in one keyless call. We use it for two things:

* the picture of the day, with Wikipedia's own licence block; and
* featured *articles*, whose lead images are frequently excellent and whose
  subject matter is already vetted for significance.

Wikipedia is a starting point, never the last word: everything discovered
here goes through the research stage, which is required to reach the
underlying institutional sources before anything is written.
"""

from __future__ import annotations

import datetime as dt
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
from ..sanitize import clean_text, strip_html
from .base import DiscoverySource, register

logger = get_logger(__name__)

FEED_URL = "https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{y}/{m:02d}/{d:02d}"


@register("wikipedia")
class WikipediaFeaturedFeed(DiscoverySource):
    authority = 70

    async def fetch(self, limit: int) -> list[Candidate]:
        today = dt.date.today()
        days = min(max(limit // 2, 1), 30)
        tasks = [self._fetch_day(today - dt.timedelta(days=offset)) for offset in range(days)]
        results = await gather_resilient(tasks, label="wikipedia feed")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _fetch_day(self, day: dt.date) -> list[Candidate]:
        data = await self.http.get_json(
            FEED_URL.format(y=day.year, m=day.month, d=day.day)
        )
        candidates: list[Candidate] = []
        if image := data.get("image"):
            if candidate := self._from_potd(image, day):
                candidates.append(candidate)
        if tfa := data.get("tfa"):
            if candidate := self._from_article(tfa):
                candidates.append(candidate)
        return candidates

    def _from_potd(self, payload: dict[str, Any], day: dt.date) -> Candidate | None:
        image_block = payload.get("image") or {}
        source_url = image_block.get("source") or (payload.get("thumbnail") or {}).get("source")
        if not source_url:
            return None
        description = clean_text(strip_html((payload.get("description") or {}).get("text", "")))
        artist = clean_text(strip_html((payload.get("artist") or {}).get("text", "")))
        credit = clean_text(strip_html((payload.get("credit") or {}).get("text", "")))
        licence = payload.get("license") or {}
        licence_raw = " ".join(
            str(part) for part in (licence.get("type"), licence.get("code")) if part
        )
        title = clean_text(payload.get("title", "")).removeprefix("File:")

        image = ImageAsset(
            url=source_url,
            page_url=payload.get("file_page"),
            thumbnail_url=(payload.get("thumbnail") or {}).get("source"),
            width=image_block.get("width"),
            height=image_block.get("height"),
            title=title,
            description=description[:1200] or None,
            creator=artist or None,
            institution=credit or "Wikimedia Commons",
            license=LicenseInfo(raw=licence_raw),
        )
        blob = f"{title} {description}"
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")
        return Candidate(
            source=self.name,
            source_url=payload.get("file_page") or source_url,
            title=title or "Picture of the day",
            description=description,
            image=image,
            categories=sorted(set(domains)),
            music=music,
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title, description),
            raw={"kind": "potd", "date": day.isoformat()},
        )

    def _from_article(self, payload: dict[str, Any]) -> Candidate | None:
        original = payload.get("originalimage") or {}
        thumbnail = payload.get("thumbnail") or {}
        source_url = original.get("source") or thumbnail.get("source")
        if not source_url:
            return None
        title = clean_text(payload.get("titles", {}).get("normalized") or payload.get("title", ""))
        extract = clean_text(payload.get("extract", ""), max_chars=1500)

        # A featured article's lead image carries no licence block in this
        # feed. Rather than guess, we record the article as the lead and let
        # the Commons lookup in the research stage resolve the licence; until
        # then the licence gate will drop it. That is the correct default.
        image = ImageAsset(
            url=source_url,
            page_url=payload.get("content_urls", {}).get("desktop", {}).get("page"),
            thumbnail_url=thumbnail.get("source"),
            width=original.get("width") or thumbnail.get("width"),
            height=original.get("height") or thumbnail.get("height"),
            title=title,
            description=extract[:1200] or None,
            institution="Wikipedia",
            license=LicenseInfo(raw=payload.get("image_license") or ""),
        )
        blob = f"{title} {extract}"
        music = detect_music(blob, config=self.config.music)
        domains = classify_domains(blob, allowed=self.config.content.categories)
        if music and "music" not in domains:
            domains.append("music")
        return Candidate(
            source=self.name,
            source_url=image.page_url or source_url,
            title=title,
            description=extract,
            image=image,
            categories=sorted(set(domains)),
            music=music,
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title, extract),
            raw={"kind": "tfa"},
        )


__all__ = ["WikipediaFeaturedFeed"]
