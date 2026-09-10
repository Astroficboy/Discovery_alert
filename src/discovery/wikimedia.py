"""Wikimedia Commons.

Commons is the backbone of this project: it is enormous, it is keyless, its
metadata includes machine-readable licensing, and its Featured/Quality picture
categories are already curated for visual quality by people who care about it.

Three sources live here:

* ``wikimedia_potd``      - the Picture of the Day for the last N days.
* ``wikimedia_featured``  - members of Featured-picture categories.
* ``wikimedia_music``     - the same machinery pointed at music categories, so
  music genuinely competes for every edition rather than being bolted on.

All three share :class:`_CommonsSource`, which does the API call and the
``extmetadata`` parsing. Commons' ``extmetadata`` values are HTML fragments,
so everything that comes out of them goes through :mod:`src.sanitize`.
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

COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def _meta(extmetadata: dict[str, Any], key: str) -> str:
    """Read one ``extmetadata`` field as clean plain text."""
    entry = extmetadata.get(key)
    if not isinstance(entry, dict):
        return ""
    return strip_html(str(entry.get("value", "")))


class _CommonsSource(DiscoverySource):
    """Shared Commons query + parse."""

    authority = 78

    async def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        base = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata|user",
            "iiurlwidth": str(self.config.image.email_display_width),
            "iiextmetadatafilter": (
                "ObjectName|ImageDescription|Artist|Credit|DateTimeOriginal|"
                "LicenseShortName|License|LicenseUrl|UsageTerms|Categories|"
                "Attribution|Permission|Restrictions"
            ),
        }
        data = await self.http.get_json(COMMONS_API, params={**base, **params})
        pages = data.get("query", {}).get("pages", [])
        return [page for page in pages if isinstance(page, dict)]

    def _to_candidate(self, page: dict[str, Any], *, extra_categories: list[str] | None = None,
                      caption: str = "") -> Candidate | None:
        info_list = page.get("imageinfo") or []
        if not info_list:
            return None
        info = info_list[0]
        if not info.get("url"):
            return None

        extmetadata = info.get("extmetadata") or {}
        file_title = str(page.get("title", "")).removeprefix("File:")
        object_name = _meta(extmetadata, "ObjectName") or file_title
        description = caption or _meta(extmetadata, "ImageDescription")
        artist = _meta(extmetadata, "Artist")
        credit = _meta(extmetadata, "Credit")
        created = _meta(extmetadata, "DateTimeOriginal")
        categories = [
            clean_text(part) for part in _meta(extmetadata, "Categories").split("|") if part.strip()
        ]

        # Prefer the machine-readable licence id, fall back to the display name
        # and then to the usage-terms prose. classify() refuses anything it
        # cannot positively identify.
        licence_raw = (
            _meta(extmetadata, "License")
            or _meta(extmetadata, "LicenseShortName")
            or _meta(extmetadata, "UsageTerms")
        )
        restrictions = _meta(extmetadata, "Restrictions")
        if restrictions:
            # e.g. trademarked, personality rights. Not a copyright bar, but we
            # note it so the quality stage can weigh it.
            description = f"{description}\n[Commons restriction note: {restrictions}]"

        title = clean_text(object_name) or file_title
        blob = " ".join([title, description, " ".join(categories)])

        image = ImageAsset(
            url=info.get("thumburl") or info["url"],
            page_url=info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{page.get('title','')}",
            thumbnail_url=info.get("thumburl"),
            width=info.get("thumbwidth") or info.get("width"),
            height=info.get("thumbheight") or info.get("height"),
            mime_type=info.get("mime"),
            size_bytes=info.get("size"),
            title=title,
            description=clean_text(description, max_chars=1200),
            creator=clean_text(artist) or info.get("user"),
            created=clean_text(created)[:60] or None,
            institution=clean_text(credit)[:120] or "Wikimedia Commons",
            license=LicenseInfo(raw=licence_raw, url=_meta(extmetadata, "LicenseUrl") or None),
        )
        # The email links the width-capped rendering; keep the original on the
        # record so the reader (and the archive page) can reach full resolution.
        if info.get("thumburl") and info.get("url") != info.get("thumburl"):
            image.description = image.description or None

        domains = classify_domains(blob, allowed=self.config.content.categories)
        domains.extend(extra_categories or [])
        music = detect_music(blob, config=self.config.music)
        if music and "music" not in domains:
            domains.append("music")

        return Candidate(
            source=self.name,
            source_url=image.page_url or COMMONS_API,
            title=title,
            description=image.description or "",
            image=image,
            categories=sorted(set(domains)),
            music=music,
            keywords=keywords_from_text(blob),
            entities=entities_from_text(title, description),
            date_hint=image.created,
            raw={"commons_categories": categories[:20], "pageid": page.get("pageid")},
        )


@register("wikimedia_potd")
class WikimediaPictureOfTheDay(_CommonsSource):
    """Commons Picture of the Day, walked backwards from today.

    POTD is human-selected for visual impact, which is exactly the filter we
    would otherwise have to build ourselves.
    """

    authority = 82

    async def fetch(self, limit: int) -> list[Candidate]:
        today = dt.date.today()
        days = min(limit, 60)
        tasks = [self._fetch_day(today - dt.timedelta(days=offset)) for offset in range(days)]
        results = await gather_resilient(tasks, label="commons potd")
        candidates: list[Candidate] = []
        for batch in results:
            candidates.extend(batch)
        return candidates[:limit]

    async def _fetch_day(self, day: dt.date) -> list[Candidate]:
        # The Potd template transcludes exactly the day's file, so using it as
        # an image generator is more stable than parsing wikitext.
        pages = await self._query({
            "generator": "images",
            "titles": f"Template:Potd/{day.isoformat()}",
            "gimlimit": "5",
        })
        caption = await self._caption(day)
        out = []
        for page in pages:
            candidate = self._to_candidate(page, caption=caption)
            if candidate:
                candidate.raw["potd_date"] = day.isoformat()
                out.append(candidate)
        return out

    async def _caption(self, day: dt.date) -> str:
        """The English POTD caption is a short editorial line - a useful hook."""
        try:
            data = await self.http.get_json(COMMONS_API, params={
                "action": "parse", "format": "json", "formatversion": "2",
                "page": f"Template:Potd/{day.isoformat()} (en)", "prop": "text",
                "disablelimitreport": "1",
            })
        except Exception:  # noqa: BLE001 - the caption is a nicety
            return ""
        return clean_text(strip_html(data.get("parse", {}).get("text", "")), max_chars=600)


@register("wikimedia_featured")
class WikimediaFeatured(_CommonsSource):
    """Members of Commons Featured-picture categories."""

    authority = 76
    DEFAULT_CATEGORIES = (
        "Featured pictures of natural phenomena",
        "Featured pictures of history",
        "Featured pictures of science",
        "Featured pictures of engineering and technology",
    )

    def _categories(self) -> list[str]:
        return list(self.options.get("categories") or self.DEFAULT_CATEGORIES)

    async def fetch(self, limit: int) -> list[Candidate]:
        categories = self._categories()
        if not categories:
            return []
        per_category = max(3, limit // len(categories) + 1)
        tasks = [self._fetch_category(name, per_category) for name in categories]
        results = await gather_resilient(tasks, label=f"{self.name} category")
        return _interleave(results)[:limit]

    async def _fetch_category(self, category: str, limit: int) -> list[Candidate]:
        title = category if category.lower().startswith("category:") else f"Category:{category}"
        pages = await self._query({
            "generator": "categorymembers",
            "gcmtitle": title,
            "gcmtype": "file",
            "gcmlimit": str(min(limit, 50)),
            # Sorting by timestamp gives us fresh material each run rather than
            # the same alphabetical head every time.
            "gcmsort": "timestamp",
            "gcmdir": "desc",
        })
        out = []
        for page in pages:
            candidate = self._to_candidate(page, extra_categories=self._category_hint(category))
            if candidate:
                candidate.raw["commons_source_category"] = category
                out.append(candidate)
        return out

    def _category_hint(self, category: str) -> list[str]:
        """Map a Commons category name onto our own domains, cheaply."""
        return classify_domains(category, allowed=self.config.content.categories)


@register("wikimedia_music")
class WikimediaMusic(WikimediaFeatured):
    """The same Commons machinery, pointed at music.

    Kept as its own source (rather than more categories on the featured one)
    so that music has an independent, tunable supply of candidates and cannot
    be crowded out by a good week in astronomy.
    """

    authority = 74
    music_focused = True
    DEFAULT_CATEGORIES = (
        "Musical instruments in museums",
        "Recording studios",
        "Synthesizers",
        "Percussion instruments",
        "Concert photographs",
    )

    def _category_hint(self, category: str) -> list[str]:
        hints = super()._category_hint(category)
        return sorted(set(hints) | {"music"})


def _interleave(batches: list[list[Candidate]]) -> list[Candidate]:
    """Round-robin across categories so one prolific category cannot fill the
    whole quota."""
    out: list[Candidate] = []
    index = 0
    while True:
        added = False
        for batch in batches:
            if index < len(batch):
                out.append(batch[index])
                added = True
        if not added:
            return out
        index += 1


__all__ = ["WikimediaFeatured", "WikimediaMusic", "WikimediaPictureOfTheDay"]
