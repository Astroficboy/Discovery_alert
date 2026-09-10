"""Quality control, before anything is sent.

Two halves, and the mechanical one runs first because it is free and because
some failures should not be a matter of opinion:

**Deterministic checks** - length inside the configured band; attribution
present; enough sources, of sufficient authority; every source link
well-formed; no markdown or HTML leaking into the prose; the image large
enough and of an allowed type; safety terms; a hook that is not one of the
banned openings; obvious padding and repetition.

**Model review** - accuracy against the dossier, sourcing, writing quality,
image fit, and whether the prose asserts anything the research does not
support.

If the combined verdict is below threshold the edition is rejected. The
pipeline then asks for one rewrite against the specific complaints, and if
that also fails it moves to the runner-up candidate. If nothing clears the
bar, nothing is sent. That is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import Config
from ..llm.base import LLMClient, LLMError
from ..llm.prompts import QUALITY_SYSTEM, quality_prompt
from ..logging_setup import Stage, get_logger, stage
from ..models import Article, Candidate, QualityReport, ResearchDossier
from ..research.sources import AUTHORITATIVE_THRESHOLD

logger = get_logger(__name__)

#: Openings that signal a machine wrote it.
_BANNED_OPENINGS = (
    "in today's world", "in a world", "imagine", "picture this", "did you know",
    "have you ever", "throughout history", "since the dawn of", "it is no secret",
    "in the annals of", "let me tell you",
)

_AI_TICS = (
    "delve", "tapestry", "testament to", "stands as a", "it's worth noting",
    "it is worth noting", "little did they know", "game-chang", "in conclusion",
    "furthermore,", "moreover,", "navigate the complexities", "rich history",
    "boasts a", "a fascinating", "truly remarkable",
)

_GRAPHIC_TERMS = (
    "mutilated", "dismembered", "disembowel", "charred remains", "close-up of the body",
)

_MARKUP = re.compile(r"<[a-z/][^>]*>|\[[^\]]+\]\([^)]+\)|^#{1,6}\s", re.IGNORECASE | re.MULTILINE)
_SENTENCE = re.compile(r"[^.!?]+[.!?]")


@dataclass
class QualityReviewer:
    config: Config
    llm: LLMClient

    async def review(self, candidate: Candidate, article: Article,
                     dossier: ResearchDossier) -> QualityReport:
        with stage(Stage.QUALITY):
            report = self.mechanical_checks(candidate, article, dossier)
            model_report = await self._model_review(candidate, article, dossier)

            # The model's scores are advisory and may only ever lower a
            # mechanical one. A reviewer that likes the picture cannot undo
            # "this licence is not reusable".
            report.accuracy = min(report.accuracy, model_report.get("accuracy", 100.0))
            report.sourcing = min(report.sourcing, model_report.get("sourcing", 100.0))
            report.writing = min(report.writing, model_report.get("writing", 100.0))
            report.image_fit = min(report.image_fit, model_report.get("image_fit", 100.0))
            report.safety_ok = report.safety_ok and bool(model_report.get("safety_ok", True))
            report.issues.extend(model_report.get("issues", []))
            report.fixes_requested.extend(model_report.get("fixes_requested", []))
            report.notes = str(model_report.get("notes", ""))[:600]

            for claim in model_report.get("unsupported_claims", []):
                report.issues.append(f"unsupported in prose: {claim}")
                report.accuracy = min(report.accuracy, 55.0)

            report.passed = self._verdict(report)
            logger.info(
                "quality review complete",
                passed=report.passed, accuracy=round(report.accuracy),
                sourcing=round(report.sourcing), writing=round(report.writing),
                issues=len(report.issues),
            )
            return report

    # ------------------------------------------------------------------ #
    def mechanical_checks(self, candidate: Candidate, article: Article,
                          dossier: ResearchDossier) -> QualityReport:
        """Free, deterministic, and not negotiable."""
        issues: list[str] = []
        blocking: list[str] = []
        fixes: list[str] = []
        quality = self.config.quality
        words = self.config.content.story_word_count

        # -- length ----------------------------------------------------- #
        count = article.word_count
        if count < words.hard_min:
            blocking.append(f"far too short: {count} words (floor is {words.hard_min})")
            fixes.append(f"expand the story to at least {words.min} words")
        elif count > words.hard_max:
            blocking.append(f"far too long: {count} words (ceiling is {words.hard_max})")
            fixes.append(f"cut to no more than {words.max} words")
        elif not (words.min <= count <= words.max):
            issues.append(f"outside the target band: {count} words "
                          f"({words.min}-{words.max})")

        # -- attribution and sourcing ----------------------------------- #
        if quality.require_image_attribution and not candidate.image.credit:
            blocking.append("no image credit line")
        if quality.require_image_attribution and \
                candidate.image.license.requires_attribution and not candidate.image.creator:
            blocking.append("licence requires attribution but no creator is recorded")
        if len(article.sources) < quality.min_sources:
            blocking.append(f"only {len(article.sources)} sources "
                            f"(minimum {quality.min_sources})")
        authoritative = [s for s in article.sources if s.authority >= AUTHORITATIVE_THRESHOLD]
        if not authoritative:
            blocking.append("no authoritative source among the references")
        for source in article.sources:
            parsed = urlparse(source.url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                blocking.append(f"malformed source URL: {source.url[:80]}")
        if quality.require_source_links and not article.sources:
            blocking.append("the edition carries no source links at all")

        # -- writing ---------------------------------------------------- #
        writing = 100.0
        opening = article.hook.strip().lower()
        for banned in _BANNED_OPENINGS:
            if opening.startswith(banned):
                issues.append(f"opens with a banned construction: “{banned}”")
                fixes.append("rewrite the opening to start on a concrete detail")
                writing -= 22
                break
        tics = [tic for tic in _AI_TICS if tic in article.body_text.lower()]
        if tics:
            issues.append(f"stock phrasing: {', '.join(tics[:4])}")
            fixes.append(f"remove these phrases: {', '.join(tics[:4])}")
            writing -= min(len(tics) * 7, 25)
        if _MARKUP.search(article.body_text):
            issues.append("markdown or HTML leaked into the prose")
            fixes.append("return plain text only")
            writing -= 12
        repetition = self._repetition(article.body_text)
        if repetition > 0.14:
            issues.append(f"repetitive phrasing ({repetition:.0%} of sentences reuse an opening)")
            writing -= 15
        if article.hook and len(article.hook.split()) > 90:
            issues.append("the hook is a paragraph, not a hook")
            writing -= 8
        if not article.one_more_thing.strip():
            issues.append("no 'one more thing'")
            writing -= 10

        # -- image ------------------------------------------------------ #
        image_fit = 100.0
        image = candidate.image
        if image.width and image.width < self.config.image.min_width:
            issues.append(f"image is only {image.width}px wide")
            image_fit -= 30
        if image.mime_type and image.mime_type not in self.config.image.allowed_mime:
            issues.append(f"unsupported image type for email: {image.mime_type}")
            image_fit -= 25
        if not image.license.reusable:
            blocking.append("image licence is not clearly reusable")
            image_fit = 0.0

        # -- safety ----------------------------------------------------- #
        safety_ok = True
        haystack = f"{article.body_text} {candidate.title} {image.description or ''}".lower()
        if self.config.content.safety.avoid_graphic_imagery:
            hits = [term for term in _GRAPHIC_TERMS if term in haystack]
            if hits:
                blocking.append(f"graphic material without editorial justification: {hits[0]}")
                safety_ok = False
        for term in self.config.content.safety.banned_terms:
            if term.lower() in haystack:
                blocking.append(f"banned term present: {term}")
                safety_ok = False
        needs_note = [
            term for term in self.config.content.safety.require_content_note_for
            if term.replace("_", " ") in haystack
        ]
        if needs_note and not article.content_note:
            issues.append(f"content note required ({needs_note[0]}) but none was written")

        # -- sourcing score --------------------------------------------- #
        sourcing = 100.0
        if len(article.sources) < quality.min_sources:
            sourcing -= 30
        if not authoritative:
            sourcing -= 35
        supported = [c for c in dossier.claims if c.supporting_urls]
        if dossier.claims:
            sourcing -= 25 * (1 - len(supported) / len(dossier.claims))
        if dossier.errors:
            sourcing -= min(len(dossier.errors) * 4, 12)

        return QualityReport(
            accuracy=100.0,
            sourcing=max(0.0, sourcing),
            writing=max(0.0, writing),
            image_fit=max(0.0, image_fit),
            safety_ok=safety_ok,
            issues=[*blocking, *issues],
            blocking_issues=blocking,
            fixes_requested=fixes,
        )

    async def _model_review(self, candidate: Candidate, article: Article,
                            dossier: ResearchDossier) -> dict:
        payload = {
            "title": article.title,
            "subtitle": article.subtitle,
            "hook": article.hook,
            "the_image": article.the_image,
            "story": article.story,
            "bigger_picture": article.bigger_picture,
            "one_more_thing": article.one_more_thing,
            "listen": article.listen.model_dump() if article.listen else None,
            "content_note": article.content_note,
            "word_count": article.word_count,
            "sources": [{"title": s.title, "url": s.url} for s in article.sources],
        }
        words = self.config.content.story_word_count
        try:
            raw = await self.llm.complete_json(
                system=QUALITY_SYSTEM,
                user=quality_prompt(candidate, payload, dossier, words.min, words.max),
                purpose="quality",
                temperature=self.config.llm.analysis_temperature,
                max_tokens=2048,
            )
        except LLMError as exc:
            logger.warning("model quality review failed; mechanical checks only",
                           error=str(exc))
            # A failed reviewer must not silently wave an edition through.
            return {"accuracy": 60.0, "sourcing": 60.0, "writing": 60.0, "image_fit": 60.0,
                    "safety_ok": True,
                    "issues": [f"editorial review could not run: {exc}"],
                    "fixes_requested": [], "unsupported_claims": [],
                    "notes": "reviewed mechanically only"}
        return {
            "accuracy": _num(raw.get("accuracy"), 0.0),
            "sourcing": _num(raw.get("sourcing"), 0.0),
            "writing": _num(raw.get("writing"), 0.0),
            "image_fit": _num(raw.get("image_fit"), 0.0),
            "safety_ok": bool(raw.get("safety_ok", True)),
            "issues": [str(i)[:300] for i in (raw.get("issues") or [])][:12],
            "fixes_requested": [str(f)[:300] for f in (raw.get("fixes_requested") or [])][:8],
            "unsupported_claims": [str(c)[:300] for c in (raw.get("unsupported_claims") or [])][:8],
            "notes": str(raw.get("notes", ""))[:600],
        }

    def _verdict(self, report: QualityReport) -> bool:
        quality = self.config.quality
        if report.blocking_issues:
            return False
        if not report.safety_ok:
            return False
        if report.image_fit <= 0:
            return False
        return (
            report.accuracy >= quality.min_accuracy
            and report.writing >= quality.min_writing
            and report.sourcing >= quality.min_sourcing
        )

    @staticmethod
    def _repetition(text: str) -> float:
        """Fraction of sentences that begin with an opening already used."""
        sentences = [s.strip() for s in _SENTENCE.findall(text) if len(s.split()) > 4]
        if len(sentences) < 6:
            return 0.0
        openings = [" ".join(s.lower().split()[:3]) for s in sentences]
        seen: set[str] = set()
        repeats = 0
        for opening in openings:
            if opening in seen:
                repeats += 1
            seen.add(opening)
        return repeats / len(openings)

    def overall(self, report: QualityReport) -> float:
        """A single 0-100 number, for logging and the run record."""
        return round(
            0.40 * report.accuracy + 0.25 * report.sourcing
            + 0.25 * report.writing + 0.10 * report.image_fit,
            2,
        )


def _num(value: object, default: float) -> float:
    try:
        return max(0.0, min(100.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


__all__ = ["QualityReviewer"]
