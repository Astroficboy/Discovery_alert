"""Discovery: turning public archives into a pool of candidate editions.

Importing this package registers every built-in source. The orchestrator
:func:`discover_candidates` runs them concurrently, isolates failures, drops
exact duplicates across sources and tops up from the bundled fixtures if the
live archives came back thin.
"""

from __future__ import annotations

import asyncio

from ..config import Config
from ..logging_setup import Stage, get_logger, stage
from ..models import Candidate
from ..net import HttpClient

# Importing the modules is what registers them.
from . import (  # noqa: E402,F401
    europeana,
    fixtures,
    loc,
    met,
    museums,
    nasa,
    openverse,
    smithsonian,
    wikimedia,
    wikipedia,
)
from .base import (  # noqa: F401  (re-exported)
    DiscoverySource,
    SourceUnavailable,
    available_sources,
    build_sources,
    register,
)

logger = get_logger(__name__)

#: Below this fraction of the discovery target, we consider the run starved
#: and let the bundled fixtures contribute.
STARVATION_RATIO = 0.15

#: No single fallback source may grow beyond this multiple of its configured
#: limit, however many keyed sources are missing. One archive dominating the
#: candidate pool is its own kind of failure.
MAX_FALLBACK_MULTIPLE = 3.0


def redistribute_quota(sources: list[DiscoverySource]) -> dict[str, int]:
    """Hand the quota of unavailable keyed sources to the keyless ones.

    A missing ``SMITHSONIAN_API_KEY`` should cost breadth, not editions. The
    candidates that source would have contributed are instead requested from
    the keyless search and museum sources, which need no credentials and whose
    results still carry the explicit licences the copyright gate requires.

    Returns a map of source name to the number of extra candidates granted,
    for logging. Mutates each source's configured limit in place.
    """
    unavailable = [s for s in sources if not s.available]
    if not unavailable:
        return {}
    fallbacks = [s for s in sources if s.fallback and s.available]
    if not fallbacks:
        logger.warning(
            "sources are unavailable and no keyless fallback is enabled; "
            "discovery will be narrower than configured",
            unavailable=",".join(s.name for s in unavailable),
        )
        return {}

    orphaned = sum(s.source_config.limit for s in unavailable)
    share = max(1, orphaned // len(fallbacks))
    granted: dict[str, int] = {}
    for source in fallbacks:
        original = source.source_config.limit
        ceiling = int(original * MAX_FALLBACK_MULTIPLE)
        source.source_config.limit = min(original + share, ceiling)
        if extra := source.source_config.limit - original:
            granted[source.name] = extra

    logger.info(
        "redistributed quota from unavailable sources to keyless ones",
        unavailable=",".join(s.name for s in unavailable),
        orphaned=orphaned,
        granted=",".join(f"{name}+{n}" for name, n in sorted(granted.items())) or "none",
    )
    return granted


async def discover_candidates(config: Config, http: HttpClient) -> list[Candidate]:
    """Run every enabled source and return a de-duplicated candidate pool."""
    with stage(Stage.DISCOVERY):
        sources = build_sources(config, http)
        if not sources:
            logger.error("no discovery sources are enabled")
            return []

        redistribute_quota([s for s in sources if s.name != "fixtures"])

        logger.info("starting discovery", sources=len(sources),
                    target=config.pipeline.discovery_target)
        live = [s for s in sources if s.name != "fixtures"]
        fallback = next((s for s in sources if s.name == "fixtures"), None)

        batches = await asyncio.gather(*(s.discover() for s in live))
        candidates = _merge(batches)

        target = config.pipeline.discovery_target
        if fallback is not None and len(candidates) < max(1, int(target * STARVATION_RATIO)):
            logger.warning("live archives under-delivered; falling back to bundled fixtures",
                           live_candidates=len(candidates), target=target)
            fallback.starved = True  # type: ignore[attr-defined]
            candidates = _merge([candidates, await fallback.discover()])

        by_source: dict[str, int] = {}
        for candidate in candidates:
            by_source[candidate.source] = by_source.get(candidate.source, 0) + 1
        logger.info("discovery complete", total=len(candidates),
                    breakdown=",".join(f"{k}:{v}" for k, v in sorted(by_source.items())))
        return candidates[:target]


def _merge(batches: list[list[Candidate]]) -> list[Candidate]:
    """Interleave sources and drop exact duplicates.

    Interleaving matters: it means a single prolific archive cannot fill the
    prefilter budget and squeeze every other institution out of the running.
    """
    seen_ids: set[str] = set()
    seen_images: set[str] = set()
    out: list[Candidate] = []
    index = 0
    while True:
        added = False
        for batch in batches:
            if index >= len(batch):
                continue
            added = True
            candidate = batch[index]
            image_key = candidate.image.url.split("?")[0].lower()
            if candidate.id in seen_ids or image_key in seen_images:
                continue
            seen_ids.add(candidate.id)
            seen_images.add(image_key)
            out.append(candidate)
        if not added:
            return out
        index += 1


__all__ = [
    "DiscoverySource",
    "SourceUnavailable",
    "available_sources",
    "build_sources",
    "discover_candidates",
    "redistribute_quota",
    "register",
]
