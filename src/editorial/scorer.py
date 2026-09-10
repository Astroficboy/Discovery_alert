"""Scoring, in two tiers.

**Prefilter** (free, no model): runs on everything discovered. Pure signals -
image dimensions, licence quality, metadata richness, source authority,
whether the description contains anything that provokes a question. Its job
is to throw away the eighty percent that were never going to make an edition,
so that tokens are only spent on the rest.

**Full scoring** (one model call per finalist): the editorial judgement -
visual impact, story, novelty, significance, curiosity - blended with the
mechanical signals and the rotation and duplicate adjustments.

The weights live in ``config.yaml`` and are normalised at load, so you can
retune the newsletter's taste without touching this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..llm.base import LLMClient, LLMError
from ..llm.prompts import SCORING_SYSTEM, scoring_prompt
from ..logging_setup import Stage, get_logger, stage
from ..models import Candidate, ResearchDossier, ScoreCard
from ..research.sources import AUTHORITATIVE_THRESHOLD
from .rotation import RotationState, cross_domain_bonus, rotation_bonus

logger = get_logger(__name__)

#: Words in a caption that suggest the image is hiding a story rather than
#: simply being a picture of a thing.
_CURIOSITY_MARKERS = (
    "abandoned", "only known", "last known", "first", "never", "unknown",
    "mystery", "unexplained", "lost", "rediscovered", "buried", "hidden",
    "secret", "failed", "disaster", "survived", "rescue", "invented",
    "prototype", "experiment", "banned", "destroyed", "salvaged", "wreck",
    "discovered", "unusual", "strange", "record", "largest", "smallest",
    "deepest", "highest", "oldest", "improvised", "handmade", "one-off",
    "shortly before", "days before", "hours after", "moments after",
)

_GENERIC_MARKERS = (
    "stock photo", "logo", "screenshot", "icon", "clip art", "diagram of",
    "map of the", "flag of", "coat of arms", "official portrait",
)

_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")


# --------------------------------------------------------------------------- #
# Tier 1: free prefiltering
# --------------------------------------------------------------------------- #
def prefilter_score(candidate: Candidate, config: Config,
                    source_authority: dict[str, int] | None = None) -> tuple[float, list[str]]:
    """0-100 from metadata alone. No model, no network."""
    reasons: list[str] = []
    score = 40.0
    image = candidate.image
    text = f"{candidate.title} {candidate.description}".lower()

    # -- image ---------------------------------------------------------- #
    megapixels = image.megapixels
    if megapixels >= 6:
        score += 10
        reasons.append("high resolution")
    elif megapixels >= 2:
        score += 6
    elif megapixels > 0:
        score += 2
    else:
        score -= 4
        reasons.append("dimensions unknown")

    ratio = image.aspect_ratio
    if ratio and not (0.4 <= ratio <= 2.6):
        score -= 6
        reasons.append("awkward aspect ratio for email")

    # -- licence -------------------------------------------------------- #
    if image.license.is_public_domain:
        score += 8
        reasons.append("public domain")
    elif image.license.reusable and not image.license.share_alike:
        score += 5
    elif image.license.reusable:
        score += 3

    # -- provenance ----------------------------------------------------- #
    authority = (source_authority or {}).get(candidate.source, 60)
    score += (authority - 60) * 0.18
    if image.creator:
        score += 3
    if image.institution:
        score += 2
    if image.created or candidate.date_hint:
        score += 3
        reasons.append("dated")
    if _YEAR.search(text):
        score += 2

    # -- editorial signal ----------------------------------------------- #
    description_length = len(candidate.description or "")
    if description_length > 400:
        score += 6
        reasons.append("rich caption")
    elif description_length > 120:
        score += 3
    else:
        score -= 6
        reasons.append("thin caption")

    curiosity_hits = [word for word in _CURIOSITY_MARKERS if word in text]
    if curiosity_hits:
        score += min(len(curiosity_hits) * 3.5, 12)
        reasons.append(f"question-raising language ({', '.join(curiosity_hits[:3])})")

    if any(marker in text for marker in _GENERIC_MARKERS):
        score -= 18
        reasons.append("looks like generic or functional imagery")

    domains = set(candidate.categories)
    if len(domains) >= 3:
        score += 6
        reasons.append("spans several domains")
    elif len(domains) == 2:
        score += 3
    elif not domains:
        score -= 5
        reasons.append("no domain identified")

    if candidate.is_music:
        score += 2
        if candidate.music and candidate.music.subgenre:
            score += 2
            reasons.append("specific musical genre identified")

    if candidate.entities:
        score += min(len(candidate.entities), 4)

    return max(0.0, min(100.0, score)), reasons


def apply_prefilter(candidates: list[Candidate], config: Config,
                    source_authority: dict[str, int] | None = None) -> list[Candidate]:
    """Score, sort, cut. Returns the survivors, best first."""
    with stage(Stage.PREFILTER):
        for candidate in candidates:
            candidate.prefilter_score, candidate.prefilter_reasons = prefilter_score(
                candidate, config, source_authority
            )
        threshold = config.content.min_prefilter_score
        passing = [c for c in candidates if c.prefilter_score >= threshold]
        passing.sort(key=lambda c: -c.prefilter_score)
        kept = passing[: config.pipeline.prefilter_keep]
        logger.info("prefilter complete", scored=len(candidates), above_threshold=len(passing),
                    kept=len(kept), threshold=threshold)
        return kept


# --------------------------------------------------------------------------- #
# Tier 2: full editorial scoring
# --------------------------------------------------------------------------- #
@dataclass
class Scorer:
    config: Config
    llm: LLMClient
    rotation: RotationState

    async def score(self, candidate: Candidate, dossier: ResearchDossier,
                    fact_check: dict[str, Any] | None,
                    recent_titles: list[str]) -> ScoreCard:
        with stage(Stage.SCORING):
            try:
                payload = await self.llm.complete_json(
                    system=SCORING_SYSTEM,
                    user=scoring_prompt(candidate, dossier, fact_check, recent_titles),
                    purpose="score",
                    temperature=self.config.llm.analysis_temperature,
                    max_tokens=1536,
                )
            except LLMError as exc:
                logger.warning("editorial scoring failed; falling back to mechanical score",
                               error=str(exc), candidate_id=candidate.id)
                payload = {}

            card = self._build(candidate, dossier, payload)
            card.overall = self.combine(candidate, card)
            logger.info("scored", candidate_id=candidate.id, title=candidate.title[:60],
                        overall=round(card.overall, 1))
            return card

    # ------------------------------------------------------------------ #
    def _build(self, candidate: Candidate, dossier: ResearchDossier,
               payload: dict[str, Any]) -> ScoreCard:
        def value(key: str, default: float = 0.0) -> float:
            raw = payload.get(key, default)
            try:
                return max(0.0, min(100.0, float(raw)))
            except (TypeError, ValueError):
                return default

        # Mechanical dimensions we compute ourselves rather than ask for: the
        # model is a poor judge of its own evidence base.
        source_quality = self._source_quality(dossier)
        image_quality = self._image_quality(candidate)
        fallback = candidate.prefilter_score

        card = ScoreCard(
            visual_score=value("visual_score", fallback),
            story_score=value("story_score", fallback),
            novelty_score=value("novelty_score", fallback),
            significance_score=value("significance_score", fallback),
            curiosity_score=value("curiosity_score", fallback),
            source_quality=source_quality,
            image_quality=image_quality,
            historical_significance=value("historical_significance"),
            scientific_significance=value("scientific_significance"),
            emotional_impact=value("emotional_impact"),
            music_significance=value("music_significance") if candidate.is_music else None,
            cultural_significance=value("cultural_significance"),
            technical_significance=value("technical_significance"),
            genre_interest=value("genre_interest") if candidate.is_music else None,
            rationale=str(payload.get("rationale", ""))[:900],
            concerns=[str(c)[:300] for c in (payload.get("concerns") or [])][:8],
        )

        # Music editions get their extra dimensions folded into the generic
        # ones, so a single set of weights still governs the final number.
        if candidate.is_music:
            weights = self.config.scoring.music_weights
            music_blend = (
                weights.get("music_significance", 0.4) * (card.music_significance or 0)
                + weights.get("cultural_significance", 0.3) * (card.cultural_significance or 0)
                + weights.get("technical_significance", 0.3) * (card.technical_significance or 0)
            )
            if music_blend > 0:
                card.significance_score = round(
                    0.5 * card.significance_score + 0.5 * music_blend, 2
                )
            if card.genre_interest:
                card.story_score = round(0.85 * card.story_score + 0.15 * card.genre_interest, 2)

        card.cross_domain_bonus = cross_domain_bonus(candidate, self.config)
        card.rotation_bonus = rotation_bonus(candidate, self.rotation, self.config)
        card.duplicate_penalty = float(candidate.raw.get("duplicate_penalty", 0.0))

        for concern in self._mechanical_concerns(candidate, dossier):
            if concern not in card.concerns:
                card.concerns.append(concern)
        return card

    def combine(self, candidate: Candidate, card: ScoreCard) -> float:
        """Weighted blend plus bonuses, minus the duplicate penalty."""
        weights = self.config.scoring.weights
        base = sum(
            weights.get(name, 0.0) * getattr(card, name, 0.0)
            for name in weights
        )
        total = base + card.cross_domain_bonus + card.rotation_bonus - card.duplicate_penalty
        return round(max(0.0, min(100.0, total)), 2)

    # -- mechanical dimensions ------------------------------------------ #
    @staticmethod
    def _source_quality(dossier: ResearchDossier) -> float:
        if not dossier.sources:
            return 0.0
        authorities = sorted((s.authority for s in dossier.sources), reverse=True)
        best = authorities[0]
        # Depth matters as well as height: three good sources beat one great one.
        depth = min(len([a for a in authorities if a >= AUTHORITATIVE_THRESHOLD]), 4)
        publishers = len({s.publisher for s in dossier.sources})
        score = best * 0.6 + depth * 7.5 + min(publishers, 5) * 2.0
        supported = [c for c in dossier.claims if c.supporting_urls]
        if dossier.claims:
            score += 10 * (len(supported) / len(dossier.claims))
        return round(max(0.0, min(100.0, score)), 2)

    def _image_quality(self, candidate: Candidate) -> float:
        image = candidate.image
        score = 45.0
        megapixels = image.megapixels
        if megapixels >= 12:
            score += 30
        elif megapixels >= 6:
            score += 24
        elif megapixels >= 2:
            score += 16
        elif megapixels > 0:
            score += 6
        else:
            score -= 10
        ratio = image.aspect_ratio
        if ratio and 0.6 <= ratio <= 2.0:
            score += 8
        if image.mime_type in self.config.image.allowed_mime:
            score += 5
        if image.license.is_public_domain:
            score += 8
        elif image.license.reusable:
            score += 4
        if image.creator and image.institution:
            score += 5
        return round(max(0.0, min(100.0, score)), 2)

    def _mechanical_concerns(self, candidate: Candidate,
                             dossier: ResearchDossier) -> list[str]:
        concerns: list[str] = []
        if dossier.errors:
            concerns.append(f"{len(dossier.errors)} retrieval or model error(s) during research")
        speculative = [c for c in dossier.claims if c.confidence in ("speculative", "legend")]
        if dossier.claims and len(speculative) > len(dossier.claims) / 2:
            concerns.append("most claims are speculative or legendary")
        if dossier.content_notes:
            concerns.append(f"content note: {dossier.content_notes[0][:140]}")
        if not candidate.image.credit:
            concerns.append("no attribution line was assembled for the image")
        return concerns


__all__ = ["Scorer", "apply_prefilter", "prefilter_score"]
