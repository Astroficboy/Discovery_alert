"""Turning a dossier into an edition.

The writer is given the dossier and nothing else - no raw pages, no search
results. By the time we get here the evidence has already been gathered,
audited and stripped of anything that could not be cited, so the writing
stage cannot reintroduce material that never survived verification.

Two structural safeguards live here rather than in the prompt:

* the sources list attached to the finished article is built **by us**, from
  the dossier, so the "Sources & Further Reading" block cannot contain a link
  the model invented; and
* the listening link on a music edition is validated against the allowlist of
  legitimate services before it is allowed anywhere near the email.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import Config
from ..logging_setup import Stage, get_logger, stage
from ..models import Article, Candidate, ListeningLink, ResearchDossier, SourceRef
from ..research.sources import rank_sources
from ..sanitize import clean_text
from .base import LLMClient, LLMError
from .prompts import writing_prompt, writing_system

logger = get_logger(__name__)

#: Services a "Listen to this" link may point at. Everything else is dropped:
#: we link to legitimate sources or we link to nothing.
LISTENING_HOSTS = frozenset({
    "folkways.si.edu", "si.edu", "loc.gov", "archive.org", "bandcamp.com",
    "open.spotify.com", "music.apple.com", "music.youtube.com", "youtube.com",
    "youtu.be", "soundcloud.com", "tidal.com", "deezer.com", "qobuz.com",
    "bbc.co.uk", "npr.org", "nts.live", "boomkat.com", "discogs.com",
    "europeana.eu", "imslp.org", "musopen.org", "sangeetnatak.gov.in",
})

_MARKDOWN = re.compile(r"^#{1,6}\s+|^\s*[-*+]\s+|\*\*|__|`{1,3}", re.MULTILINE)


@dataclass
class Writer:
    config: Config
    llm: LLMClient

    async def write(self, candidate: Candidate, dossier: ResearchDossier,
                    fact_check: dict | None = None,
                    revision_notes: list[str] | None = None) -> Article:
        with stage(Stage.WRITING):
            words = self.config.content.story_word_count
            payload = await self.llm.complete_json(
                system=writing_system(words.min, words.max),
                user=writing_prompt(candidate, dossier, fact_check,
                                    words.min, words.max, revision_notes),
                purpose="write",
                temperature=self.config.llm.temperature,
                max_tokens=self.config.llm.max_output_tokens,
            )
            article = self._build(candidate, dossier, payload)
            logger.info("draft written", title=article.title[:70],
                        words=article.word_count, sources=len(article.sources))
            return article

    # ------------------------------------------------------------------ #
    def _build(self, candidate: Candidate, dossier: ResearchDossier,
               payload: dict) -> Article:
        def field(name: str, limit: int = 8000) -> str:
            return _plain(str(payload.get(name) or ""), limit)

        title = field("title", 200) or candidate.title
        article = Article(
            title=title,
            subtitle=field("subtitle", 300),
            hook=field("hook", 1200),
            the_image=field("the_image", 2500),
            story=field("story", 12000),
            bigger_picture=field("bigger_picture", 4000),
            one_more_thing=field("one_more_thing", 2500),
            listen=self._listening_link(payload.get("listen"), candidate),
            sources=self._sources(dossier),
            content_note=field("content_note", 400) or self._content_note(dossier),
        )
        if not article.hook or not article.story:
            raise LLMError("writer returned an article with no hook or no story")
        return article

    @staticmethod
    def _sources(dossier: ResearchDossier) -> list[SourceRef]:
        """Built from the dossier, never from the model's reply.

        Sources actually cited by a surviving claim come first; the rest of
        the retrieved set follows as further reading.
        """
        cited_urls = {
            url for claim in dossier.claims for url in claim.supporting_urls
        }
        cited = [s for s in dossier.sources if s.url in cited_urls]
        others = [s for s in dossier.sources if s.url not in cited_urls]
        return rank_sources(cited) + rank_sources(others)

    def _listening_link(self, raw: object, candidate: Candidate) -> ListeningLink | None:
        if not self.config.email.music_listen_block or not isinstance(raw, dict):
            return None
        url = str(raw.get("url") or "").strip()
        if not url:
            return None
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not any(host == h or host.endswith("." + h) for h in LISTENING_HOSTS):
            logger.info("listening link dropped: not a recognised legitimate service",
                        host=host or "(none)")
            return None
        artist = clean_text(str(raw.get("artist") or ""))[:160]
        if not artist:
            return None
        return ListeningLink(
            artist=artist,
            track=clean_text(str(raw.get("track") or ""))[:200] or None,
            album=clean_text(str(raw.get("album") or ""))[:200] or None,
            year=clean_text(str(raw.get("year") or ""))[:20] or None,
            url=url,
            service=clean_text(str(raw.get("service") or "")) [:60] or host,
            note=clean_text(str(raw.get("note") or ""))[:240] or None,
        )

    @staticmethod
    def _content_note(dossier: ResearchDossier) -> str | None:
        for note in dossier.content_notes:
            if note and "injection" not in note.lower() and "contradiction" not in note.lower():
                return note[:300]
        return None


def _plain(text: str, limit: int) -> str:
    """Strip markdown the model may have added despite being asked not to."""
    cleaned = _MARKDOWN.sub("", text)
    return clean_text(cleaned, max_chars=limit)


__all__ = ["LISTENING_HOSTS", "Writer"]
