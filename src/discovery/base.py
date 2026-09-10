"""The discovery source contract.

A source's only job is to hand back :class:`~src.models.Candidate` objects
with a **usable image and honest provenance**. It does no scoring, no
research, and no writing - those are later, more expensive stages.

Adding a source is three steps:

1. Subclass :class:`DiscoverySource`, implement :meth:`fetch`.
2. Decorate it with ``@register("my_source")``.
3. Add it to ``discovery.sources`` in ``config/config.yaml``.

Everything else - concurrency, retries, the licence gate, deduplication and
error isolation - is handled for you by :meth:`DiscoverySource.discover`.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from typing import Any, ClassVar

from ..config import Config, SourceConfig
from ..licensing import vet_image
from ..logging_setup import Stage, get_logger, stage
from ..models import Candidate
from ..net import HttpClient

logger = get_logger(__name__)

_REGISTRY: dict[str, type[DiscoverySource]] = {}


def register(name: str) -> Callable[[type[DiscoverySource]], type[DiscoverySource]]:
    def decorator(cls: type[DiscoverySource]) -> type[DiscoverySource]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def available_sources() -> dict[str, type[DiscoverySource]]:
    return dict(_REGISTRY)


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot run at all (e.g. a missing API key).

    This is expected and non-fatal: the source is skipped with a log line.
    """


class DiscoverySource(abc.ABC):
    """Base class for everything that can produce candidates."""

    name: ClassVar[str] = "unnamed"
    #: Baseline trust in this source's metadata and provenance, 0-100. Feeds
    #: the ``source_quality`` scoring dimension.
    authority: ClassVar[int] = 60
    #: Set on sources that mostly yield music material, so the rotation logic
    #: can reach for them when music is under-represented.
    music_focused: ClassVar[bool] = False
    #: Name of the entry in ``config.source_api_keys`` this source needs.
    requires_key: ClassVar[str | None] = None

    def __init__(self, config: Config, source_config: SourceConfig,
                 http: HttpClient) -> None:
        self.config = config
        self.source_config = source_config
        self.http = http
        self.options: dict[str, Any] = dict(source_config.options)

    # -- to implement --------------------------------------------------- #
    @abc.abstractmethod
    async def fetch(self, limit: int) -> list[Candidate]:
        """Return up to ``limit`` raw candidates. May raise; callers isolate."""

    # -- provided ------------------------------------------------------- #
    @property
    def api_key(self) -> str | None:
        if not self.requires_key:
            return None
        return self.config.source_api_keys.get(self.requires_key)

    def check_available(self) -> None:
        if self.requires_key and not self.api_key:
            raise SourceUnavailable(
                f"{self.name}: no API key configured "
                f"({self.requires_key.upper()}_API_KEY); skipping this source"
            )

    async def discover(self) -> list[Candidate]:
        """Fetch, then apply the licence gate and the image-size floor.

        Never raises: a failing archive costs its candidates, not the run.
        """
        with stage(Stage.DISCOVERY):
            try:
                self.check_available()
            except SourceUnavailable as exc:
                logger.info(str(exc), source=self.name)
                return []
            try:
                raw = await self.fetch(self.source_config.limit)
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                logger.warning("source failed", source=self.name,
                               error=f"{type(exc).__name__}: {exc}")
                return []

            kept = self.apply_gates(raw)
            logger.info("source complete", source=self.name, fetched=len(raw), kept=len(kept))
            return kept

    def apply_gates(self, candidates: list[Candidate]) -> list[Candidate]:
        """Drop anything unusable: bad licence, missing or too-small image."""
        image_cfg = self.config.image
        kept: list[Candidate] = []
        rejected_licence = 0
        rejected_size = 0
        for candidate in candidates:
            if not candidate.image.url:
                continue
            hints = " ".join(
                filter(None, [candidate.image.institution, candidate.source, candidate.description])
            )
            usable, reason = vet_image(candidate.image, hints=hints)
            if not usable:
                rejected_licence += 1
                logger.debug("image rejected on licence", source=self.name,
                             title=candidate.title[:60], reason=reason)
                continue
            width, height = candidate.image.width, candidate.image.height
            if width and height and (width < image_cfg.min_width or height < image_cfg.min_height):
                rejected_size += 1
                continue
            kept.append(candidate)
        if rejected_licence or rejected_size:
            logger.debug("gate summary", source=self.name, licence=rejected_licence,
                         too_small=rejected_size, stage=Stage.LICENSING)
        return kept


def build_sources(config: Config, http: HttpClient) -> list[DiscoverySource]:
    """Instantiate every enabled, known source from configuration."""
    sources: list[DiscoverySource] = []
    for source_config in config.discovery.sources:
        if not source_config.enabled:
            continue
        cls = _REGISTRY.get(source_config.name)
        if cls is None:
            logger.warning("unknown discovery source in config; ignoring",
                           source=source_config.name,
                           known=",".join(sorted(_REGISTRY)))
            continue
        sources.append(cls(config, source_config, http))
    return sources


__all__ = [
    "DiscoverySource",
    "SourceUnavailable",
    "available_sources",
    "build_sources",
    "register",
]
