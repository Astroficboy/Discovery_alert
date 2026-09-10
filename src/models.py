"""Domain models shared by every stage of the pipeline.

A candidate travels the whole funnel as one object, accumulating fields:

    Candidate            (discovery)
      .prefilter         (free heuristics)
      .triage            (one cheap batched LLM call)
      .research          (network + reading)
      .scores            (full editorial scoring)
      .article           (the written edition)
      .quality           (the final review)

Nothing is mutated in place by a later stage that an earlier stage owns, which
makes the run log a readable audit trail.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- #
# Licensing / imagery
# --------------------------------------------------------------------------- #
class LicenseInfo(_Model):
    """What we are actually allowed to do with an image.

    ``reusable`` is the gate. It is only ever set by
    :mod:`src.licensing`, which works from an allowlist - an unrecognised
    licence string is never assumed to be permissive.
    """

    id: str = "unknown"
    name: str = "Unknown"
    url: str | None = None
    reusable: bool = False
    requires_attribution: bool = True
    share_alike: bool = False
    commercial_ok: bool = False
    raw: str | None = None

    @property
    def is_public_domain(self) -> bool:
        return self.id.startswith("pd") or self.id == "cc0"


class ImageAsset(_Model):
    url: str
    page_url: str | None = None
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    title: str | None = None
    description: str | None = None
    creator: str | None = None
    created: str | None = None
    institution: str | None = None
    license: LicenseInfo = Field(default_factory=LicenseInfo)
    #: Ready-to-print credit line, assembled by :func:`src.licensing.credit_line`.
    credit: str | None = None
    sha256: str | None = None

    @property
    def megapixels(self) -> float:
        if not self.width or not self.height:
            return 0.0
        return (self.width * self.height) / 1_000_000

    @property
    def aspect_ratio(self) -> float:
        if not self.width or not self.height:
            return 0.0
        return self.width / self.height


# --------------------------------------------------------------------------- #
# Music metadata
# --------------------------------------------------------------------------- #
class ListeningLink(_Model):
    """A link to hear the thing, never the audio itself."""

    artist: str
    track: str | None = None
    album: str | None = None
    year: str | None = None
    url: str
    service: str | None = None
    note: str | None = None


class MusicMeta(_Model):
    genre: list[str] = Field(default_factory=list)
    subgenre: list[str] = Field(default_factory=list)
    era: list[str] = Field(default_factory=list)
    country: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    artists: list[str] = Field(default_factory=list)
    listen: ListeningLink | None = None

    def is_empty(self) -> bool:
        return not any((self.genre, self.subgenre, self.subjects, self.artists))


# --------------------------------------------------------------------------- #
# Research
# --------------------------------------------------------------------------- #
Confidence = Literal["established", "well_evidenced", "plausible", "speculative", "legend"]


class SourceRef(_Model):
    """One reference. ``authority`` is 0-100, from :mod:`src.research.sources`."""

    title: str
    url: str
    publisher: str | None = None
    kind: str = "web"
    authority: int = 50
    accessed: datetime = Field(default_factory=utcnow)
    excerpt: str | None = None

    @property
    def domain(self) -> str:
        match = re.match(r"https?://([^/]+)", self.url)
        return match.group(1).lower() if match else ""


class Claim(_Model):
    """A single factual statement, with its evidential status attached.

    The writer is instructed to carry ``confidence`` through into the prose:
    speculation must read as speculation.
    """

    text: str
    confidence: Confidence = "plausible"
    supporting_urls: list[str] = Field(default_factory=list)
    contradicting_urls: list[str] = Field(default_factory=list)
    note: str | None = None

    @property
    def independent_support(self) -> int:
        domains = {re.sub(r"^https?://(www\.)?", "", u).split("/")[0] for u in self.supporting_urls}
        return len(domains)


class Disagreement(_Model):
    topic: str
    positions: list[str] = Field(default_factory=list)
    note: str | None = None


class ResearchDossier(_Model):
    core_question: str = ""
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    timeline: list[str] = Field(default_factory=list)
    content_notes: list[str] = Field(default_factory=list)
    pages_read: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def authoritative_source_count(self) -> int:
        return sum(1 for s in self.sources if s.authority >= 70)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class ScoreCard(_Model):
    """Every dimension is 0-100. ``overall`` is the weighted result."""

    visual_score: float = 0.0
    story_score: float = 0.0
    novelty_score: float = 0.0
    significance_score: float = 0.0
    curiosity_score: float = 0.0
    source_quality: float = 0.0
    image_quality: float = 0.0

    # Music-only dimensions, folded into the generic ones before weighting.
    music_significance: float | None = None
    cultural_significance: float | None = None
    technical_significance: float | None = None
    genre_interest: float | None = None
    emotional_impact: float | None = None
    historical_significance: float | None = None
    scientific_significance: float | None = None

    cross_domain_bonus: float = 0.0
    rotation_bonus: float = 0.0
    duplicate_penalty: float = 0.0
    overall: float = 0.0
    rationale: str = ""
    concerns: list[str] = Field(default_factory=list)

    def dimensions(self) -> dict[str, float]:
        return {
            key: value
            for key, value in self.model_dump().items()
            if isinstance(value, (int, float)) and key != "overall"
        }


class TriageVerdict(_Model):
    candidate_id: str
    interest: float = 0.0
    the_question: str = ""
    likely_angle: str = ""
    domains: list[str] = Field(default_factory=list)
    verdict: Literal["pursue", "maybe", "drop"] = "maybe"
    reason: str = ""


# --------------------------------------------------------------------------- #
# The written article
# --------------------------------------------------------------------------- #
class Article(_Model):
    title: str
    subtitle: str = ""
    hook: str
    the_image: str
    story: str
    bigger_picture: str
    one_more_thing: str
    listen: ListeningLink | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    content_note: str | None = None

    @property
    def body_text(self) -> str:
        return "\n\n".join(
            part
            for part in (self.hook, self.the_image, self.story, self.bigger_picture,
                         self.one_more_thing)
            if part
        )

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b[\w'-]+\b", self.body_text))


class QualityReport(_Model):
    accuracy: float = 0.0
    sourcing: float = 0.0
    writing: float = 0.0
    image_fit: float = 0.0
    safety_ok: bool = True
    passed: bool = False
    issues: list[str] = Field(default_factory=list)
    #: Failures that are not a matter of degree - an unusable licence, a draft
    #: half the required length, a missing credit line. Any one of these
    #: rejects the edition regardless of how well it scores elsewhere.
    blocking_issues: list[str] = Field(default_factory=list)
    fixes_requested: list[str] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------- #
# Candidate
# --------------------------------------------------------------------------- #
class Candidate(_Model):
    """One possible edition, at whatever stage of completeness."""

    id: str = ""
    source: str
    source_url: str
    title: str
    description: str = ""
    image: ImageAsset
    categories: list[str] = Field(default_factory=list)
    music: MusicMeta | None = None
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    date_hint: str | None = None
    location_hint: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=utcnow)

    prefilter_score: float = 0.0
    prefilter_reasons: list[str] = Field(default_factory=list)
    triage: TriageVerdict | None = None
    research: ResearchDossier | None = None
    scores: ScoreCard | None = None
    article: Article | None = None
    quality: QualityReport | None = None
    rejected_reason: str | None = None

    @field_validator("categories")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [c.strip().lower().replace(" ", "_") for c in v if c.strip()]

    def model_post_init(self, _ctx: Any) -> None:
        if not self.id:
            self.id = self.compute_id()

    def compute_id(self) -> str:
        """Stable identity: the image is the edition, so the image URL leads."""
        basis = f"{self.image.url}|{normalise_title(self.title)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]

    @property
    def is_music(self) -> bool:
        return "music" in self.categories or (self.music is not None and not self.music.is_empty())

    @property
    def domains(self) -> list[str]:
        return sorted(set(self.categories))

    @property
    def overall(self) -> float:
        return self.scores.overall if self.scores else self.prefilter_score


class Edition(_Model):
    """A sent (or about-to-be-sent) newsletter."""

    issue_number: int
    edition_date: date
    candidate_id: str
    title: str
    category: str
    categories: list[str] = Field(default_factory=list)
    image_url: str
    image_page_url: str | None = None
    image_credit: str | None = None
    score: float = 0.0
    music: MusicMeta | None = None
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    html_path: str | None = None
    sent_at: datetime | None = None
    status: Literal["draft", "pending_review", "sent", "failed", "skipped"] = "draft"
    rating: str | None = None


class RunRecord(_Model):
    """One execution of the pipeline, for observability."""

    execution_id: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    mode: str = "run"
    discovered: int = 0
    after_prefilter: int = 0
    after_triage: int = 0
    researched: int = 0
    selected_candidate_id: str | None = None
    selected_title: str | None = None
    selected_score: float | None = None
    issue_number: int | None = None
    email_status: str = "not_attempted"
    outcome: str = "unknown"
    errors: list[str] = Field(default_factory=list)
    stages: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Lowercase, strip punctuation and file-name noise. Used for dedup keys."""
    text = title.lower()
    text = re.sub(r"^(file|image):", "", text)
    text = re.sub(r"\.(jpe?g|png|tiff?|webp|gif)$", "", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


__all__ = [
    "Article",
    "Candidate",
    "Claim",
    "Confidence",
    "Disagreement",
    "Edition",
    "ImageAsset",
    "LicenseInfo",
    "ListeningLink",
    "MusicMeta",
    "QualityReport",
    "ResearchDossier",
    "RunRecord",
    "ScoreCard",
    "SourceRef",
    "TriageVerdict",
    "normalise_title",
    "utcnow",
]
