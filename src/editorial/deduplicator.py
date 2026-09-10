"""Not sending the same thing twice.

Four independent checks, cheapest first:

1. **Exact candidate id** - the same archive record, already used.
2. **Image identity** - the same photograph at a different size or from a
   different aggregator.
3. **Entity cooldown** - the same subject (Apollo, Abbey Road, Shackleton)
   inside a long window.
4. **Topical similarity** - a weighted blend of title, keyword and entity
   overlap against everything sent recently.

Similarity uses token sets rather than embeddings. That is a deliberate
choice for a project that should cost pennies: it needs no model call, no
vector store and no extra dependency, and for "is this basically the same
story I sent on Tuesday" it is entirely adequate. The interface below is
where a real embedding backend would slot in - see
:meth:`Deduplicator.similarity`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import Config
from ..logging_setup import Stage, get_logger
from ..models import Candidate, Edition, normalise_title
from ..storage.database import image_key

logger = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9']+")

_GENERIC = frozenset("""
photograph photo image picture view portrait scene the a an of in on at and or
from with by for its his her their this that these those first new old great
""".split())


def tokens(*texts: str | None) -> set[str]:
    blob = " ".join(t for t in texts if t).lower()
    return {t for t in _TOKEN.findall(blob) if len(t) > 2 and t not in _GENERIC}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def overlap_coefficient(left: set[str], right: set[str]) -> float:
    """Overlap over the *smaller* set. Catches "this new story is a subset of
    one I already sent", which Jaccard understates."""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


@dataclass
class DuplicateVerdict:
    is_duplicate: bool
    #: Similarity after recency decay - what the decision is actually made on.
    similarity: float
    penalty: float
    reason: str = ""
    matched_issue: int | None = None
    matched_title: str = ""
    #: Undecayed similarity, for debugging and for tuning the thresholds.
    raw_similarity: float = 0.0


@dataclass
class Deduplicator:
    """Compares a candidate against recent editions."""

    config: Config
    recent: list[Edition] = field(default_factory=list)
    used_candidate_ids: set[str] = field(default_factory=set)
    used_image_keys: set[str] = field(default_factory=set)

    @classmethod
    def from_history(cls, config: Config, editions: list[Edition]) -> Deduplicator:
        return cls(
            config=config,
            recent=editions,
            used_candidate_ids={e.candidate_id for e in editions},
            used_image_keys={image_key(e.image_url) for e in editions},
        )

    # -- pluggable similarity ------------------------------------------ #
    def similarity(self, candidate: Candidate, edition: Edition) -> float:
        """Topical similarity in ``[0, 1]``.

        Replace this method (or subclass) to use embeddings. Everything else
        in the module is written against this one signature.
        """
        title_sim = jaccard(
            tokens(normalise_title(candidate.title)),
            tokens(normalise_title(edition.title)),
        )
        keyword_sim = overlap_coefficient(
            tokens(*candidate.keywords), tokens(*edition.keywords)
        )
        entity_sim = overlap_coefficient(
            tokens(*candidate.entities), tokens(*edition.entities)
        )
        category_sim = jaccard(set(candidate.categories), set(edition.categories or []))
        music_sim = 0.0
        if candidate.music and edition.music:
            music_sim = max(
                overlap_coefficient(
                    set(candidate.music.subgenre or candidate.music.genre),
                    set(edition.music.subgenre or edition.music.genre),
                ),
                overlap_coefficient(set(candidate.music.artists), set(edition.music.artists)),
            )
        # Entities and keywords carry the signal; categories alone must never
        # make two stories look like duplicates.
        return min(1.0, (
            0.30 * title_sim
            + 0.28 * keyword_sim
            + 0.30 * entity_sim
            + 0.05 * category_sim
            + 0.07 * music_sim
        ) / 0.93)

    # -- checks --------------------------------------------------------- #
    def check(self, candidate: Candidate) -> DuplicateVerdict:
        if candidate.id in self.used_candidate_ids:
            return DuplicateVerdict(True, 1.0, self.config.scoring.duplicate_penalty_max,
                                    "this exact archive record has already been sent")
        if image_key(candidate.image.url) in self.used_image_keys:
            return DuplicateVerdict(True, 1.0, self.config.scoring.duplicate_penalty_max,
                                    "this image has already been sent")

        entity_hit = self._entity_cooldown(candidate)
        if entity_hit is not None:
            edition, shared = entity_hit
            return DuplicateVerdict(
                True, 0.9, self.config.scoring.duplicate_penalty_max,
                f"subject cooldown: '{shared}' ran in issue #{edition.issue_number}",
                edition.issue_number, edition.title,
            )

        best_score = 0.0
        best_raw = 0.0
        best_edition: Edition | None = None
        for edition in self.recent:
            raw = self.similarity(candidate, edition)
            decayed = raw * self.recency_weight(edition)
            if decayed > best_score:
                best_score, best_raw, best_edition = decayed, raw, edition

        threshold = self.config.content.duplicate_similarity_threshold
        penalty = self._penalty(best_score, threshold)
        if best_score >= threshold and best_edition is not None:
            return DuplicateVerdict(
                True, best_score, self.config.scoring.duplicate_penalty_max,
                f"too similar ({best_score:.2f}) to issue #{best_edition.issue_number}",
                best_edition.issue_number, best_edition.title, raw_similarity=best_raw,
            )
        return DuplicateVerdict(
            False, best_score, penalty,
            "" if penalty <= 0 else f"mild overlap ({best_score:.2f}) with a recent edition",
            best_edition.issue_number if best_edition else None,
            best_edition.title if best_edition else "",
            raw_similarity=best_raw,
        )

    def recency_weight(self, edition: Edition, today: date | None = None) -> float:
        """How much a past edition still constrains us, in ``[0, 1]``.

        This is what lets a subject come back. Inside ``avoid_recent_days`` an
        edition counts fully. After that its weight falls away, and past twice
        the entity cooldown it stops mattering altogether - a good Apollo story
        should be allowed to return eventually, just not next week.
        """
        age = ((today or date.today()) - edition.edition_date).days
        near = self.config.content.avoid_recent_days
        far = max(self.config.content.entity_cooldown_days, near + 1)
        if age <= near:
            return 1.0
        if age <= far:
            # 1.0 -> 0.4 across the window between the two settings.
            return 1.0 - 0.6 * ((age - near) / (far - near))
        # 0.4 -> 0.0 across the same span again.
        return max(0.0, 0.4 * (1.0 - (age - far) / (far - near)))

    #: Penalties start ramping in at this fraction of the hard threshold.
    PENALTY_ONSET = 0.3

    def _penalty(self, similarity: float, threshold: float) -> float:
        """Ramp the penalty in below the hard threshold, so "a bit similar"
        costs a candidate points without eliminating it."""
        onset = threshold * self.PENALTY_ONSET
        if similarity <= onset:
            return 0.0
        span = max(threshold - onset, 1e-6)
        ratio = min((similarity - onset) / span, 1.0)
        return round(self.config.scoring.duplicate_penalty_max * ratio, 2)

    def _entity_cooldown(self, candidate: Candidate) -> tuple[Edition, str] | None:
        """A named subject may not come back inside ``entity_cooldown_days``.

        Only reasonably specific entities count: a two-word proper noun, not
        "The" or "Museum".
        """
        cutoff = date.today() - timedelta(days=self.config.content.entity_cooldown_days)
        candidate_entities = {
            e.lower() for e in candidate.entities if len(e) >= 5 and " " not in e.strip()[:2]
        }
        if not candidate_entities:
            return None
        for edition in self.recent:
            if edition.edition_date < cutoff:
                continue
            shared = candidate_entities & {e.lower() for e in edition.entities}
            meaningful = {s for s in shared if len(s) >= 6}
            if meaningful:
                return edition, sorted(meaningful)[0]
        return None

    def annotate(self, candidates: list[Candidate]) -> list[Candidate]:
        """Attach the duplicate penalty to each candidate and drop hard
        duplicates. Returns the survivors."""
        survivors: list[Candidate] = []
        dropped = 0
        for candidate in candidates:
            verdict = self.check(candidate)
            if verdict.is_duplicate:
                candidate.rejected_reason = verdict.reason
                dropped += 1
                logger.debug("duplicate dropped", title=candidate.title[:60],
                             reason=verdict.reason, stage=Stage.DEDUP)
                continue
            if candidate.scores:
                candidate.scores.duplicate_penalty = verdict.penalty
            candidate.raw["duplicate_similarity"] = round(verdict.similarity, 3)
            candidate.raw["duplicate_penalty"] = verdict.penalty
            survivors.append(candidate)
        if dropped:
            logger.info("duplicates removed", dropped=dropped, kept=len(survivors),
                        stage=Stage.DEDUP)
        return survivors


__all__ = ["Deduplicator", "DuplicateVerdict", "jaccard", "overlap_coefficient", "tokens"]
