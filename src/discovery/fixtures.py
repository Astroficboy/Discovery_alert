"""A small bundled set of candidates, used only when the live archives fail.

Why this exists: a scheduled newsletter that produces *nothing* when an
archive has a bad morning is annoying, and a pipeline you cannot exercise
without network access is hard to develop against. This source reads
``src/fixtures/candidates.json`` and, by default
(``options.only_when_starved: true``), contributes nothing unless live
discovery came back thin.

The bundled entries reference Wikimedia Commons files through
``Special:FilePath``, which resolves a file by name rather than by content
hash, so the URLs stay valid as long as the files do. They are still put
through the licence gate and the image check like anything else - a fixture
whose file has been renamed or deleted is dropped, not published.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..editorial.taxonomy import detect_music, entities_from_text, keywords_from_text
from ..logging_setup import get_logger
from ..models import Candidate, ImageAsset, LicenseInfo, MusicMeta
from .base import DiscoverySource, register

logger = get_logger(__name__)

FIXTURE_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "candidates.json"


@register("fixtures")
class BundledFixtures(DiscoverySource):
    authority = 65

    #: Set by the discovery orchestrator when live sources under-delivered.
    starved: bool = False

    async def fetch(self, limit: int) -> list[Candidate]:
        if self.options.get("only_when_starved", True) and not self.starved:
            return []
        return self.load(limit)

    def load(self, limit: int | None = None) -> list[Candidate]:
        path = Path(self.options.get("path") or FIXTURE_FILE)
        if not path.exists():
            logger.warning("fixture file missing", path=str(path))
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("fixture file unreadable", path=str(path), error=str(exc))
            return []
        entries = payload.get("candidates", []) if isinstance(payload, dict) else payload
        out = [c for c in (self._to_candidate(e) for e in entries) if c is not None]
        return out[:limit] if limit else out

    def _to_candidate(self, entry: dict[str, Any]) -> Candidate | None:
        if not isinstance(entry, dict) or not entry.get("image_url"):
            return None
        image = ImageAsset(
            url=entry["image_url"],
            page_url=entry.get("page_url"),
            thumbnail_url=entry.get("image_url"),
            width=entry.get("width"),
            height=entry.get("height"),
            title=entry.get("image_title") or entry.get("title"),
            description=entry.get("description"),
            creator=entry.get("creator"),
            created=entry.get("created"),
            institution=entry.get("institution"),
            license=LicenseInfo(raw=entry.get("license", "")),
        )
        blob = f"{entry.get('title','')} {entry.get('description','')}"
        music_payload = entry.get("music")
        music = MusicMeta(**music_payload) if music_payload else detect_music(
            blob, config=self.config.music
        )
        return Candidate(
            source=self.name,
            source_url=entry.get("page_url") or entry["image_url"],
            title=entry.get("title", ""),
            description=entry.get("description", ""),
            image=image,
            categories=entry.get("categories", []),
            music=music,
            keywords=entry.get("keywords") or keywords_from_text(blob),
            entities=entry.get("entities") or entities_from_text(entry.get("title", "")),
            date_hint=entry.get("created"),
            location_hint=entry.get("location"),
            raw={
                "fixture": True,
                "research_leads": entry.get("research_leads", []),
                # A pre-built dossier, used only by the offline researcher.
                "research": entry.get("research"),
            },
        )


__all__ = ["BundledFixtures"]
