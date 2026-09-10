"""The Metropolitan Museum of Art Open Access collection.

Keyless, generous, and it has a Musical Instruments department of about five
thousand objects - which is why it earns a place here even though the Met is
not obviously a "photograph" archive. A ninth-century Chinese bell or a
seventeenth-century Italian harpsichord is exactly the kind of object that
makes a reader ask what they are looking at.

Only objects the Met itself marks ``isPublicDomain`` are accepted; the rest
are recorded as leads and dropped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..editorial.taxonomy import (
    classify_domains,
    detect_music,
    entities_from_text,
    keywords_from_text,
)
from ..logging_setup import get_logger
from ..models import Candidate, ImageAsset, LicenseInfo, MusicMeta
from ..net import gather_resilient
from ..sanitize import clean_text
from .base import DiscoverySource, register

logger = get_logger(__name__)

BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

#: Met department ids, from /public/collection/v1/departments.
DEPARTMENTS: dict[str, int] = {
    "american decorative arts": 1,
    "ancient near eastern art": 3,
    "arms and armor": 4,
    "asian art": 6,
    "the cloisters": 7,
    "egyptian art": 10,
    "european paintings": 11,
    "greek and roman art": 13,
    "islamic art": 14,
    "robert lehman collection": 15,
    "the libraries": 16,
    "medieval art": 17,
    "musical instruments": 18,
    "photographs": 19,
    "modern art": 21,
}


@register("met_museum")
class MetMuseum(DiscoverySource):
    authority = 86

    async def fetch(self, limit: int) -> list[Candidate]:
        names = list(self.options.get("departments") or ["Musical Instruments", "Photographs"])
        per_department = max(2, limit // max(len(names), 1))
        tasks = [self._fetch_department(name, per_department) for name in names]
        results = await gather_resilient(tasks, label="met department")
        out: list[Candidate] = []
        for batch in results:
            out.extend(batch)
        return out[:limit]

    async def _fetch_department(self, name: str, limit: int) -> list[Candidate]:
        department_id = DEPARTMENTS.get(name.strip().lower())
        if department_id is None:
            logger.warning("unknown Met department; skipping", department=name)
            return []
        search = await self.http.get_json(f"{BASE}/search", params={
            "departmentId": str(department_id),
            "hasImages": "true",
            "q": self.options.get("query", "*"),
        })
        object_ids = (search.get("objectIDs") or [])[: limit * 4]
        if not object_ids:
            return []
        # The Met has no batch endpoint; fetch a bounded number concurrently.
        semaphore = asyncio.Semaphore(6)

        async def load(object_id: int) -> Candidate | None:
            async with semaphore:
                payload = await self.http.get_json(f"{BASE}/objects/{object_id}")
            return self._to_candidate(payload, name)

        results = await gather_resilient([load(oid) for oid in object_ids[: limit * 2]],
                                         label="met object")
        return [c for c in results if c is not None][:limit]

    def _to_candidate(self, payload: dict[str, Any], department: str) -> Candidate | None:
        if not payload.get("isPublicDomain"):
            return None
        url = payload.get("primaryImage") or payload.get("primaryImageSmall")
        if not url:
            return None

        title = clean_text(payload.get("title", ""))
        pieces = [
            payload.get("objectDate"), payload.get("culture"), payload.get("period"),
            payload.get("medium"), payload.get("classification"),
            payload.get("creditLine"), payload.get("geographyType"), payload.get("country"),
        ]
        description = clean_text(" · ".join(str(p) for p in pieces if p), max_chars=900)
        creator = clean_text(payload.get("artistDisplayName") or "") or None

        image = ImageAsset(
            url=url,
            page_url=payload.get("objectURL"),
            thumbnail_url=payload.get("primaryImageSmall") or url,
            title=title,
            description=description or None,
            creator=creator,
            created=clean_text(str(payload.get("objectDate") or ""))[:60] or None,
            institution="The Metropolitan Museum of Art",
            license=LicenseInfo(raw="CC0 1.0 (Met Open Access)"),
        )
        blob = " ".join([
            title, description, department,
            " ".join(tag.get("term", "") for tag in (payload.get("tags") or [])
                     if isinstance(tag, dict)),
        ])
        music = detect_music(blob, config=self.config.music)
        if department.strip().lower() == "musical instruments" and music is None:
            music = MusicMeta(subjects=["instruments"])
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
            location_hint=clean_text(str(payload.get("country") or "")) or None,
            raw={"met_object_id": payload.get("objectID"), "department": department},
        )


__all__ = ["MetMuseum"]
