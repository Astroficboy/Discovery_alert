"""The research stage.

The expensive stage, so it runs on three candidates rather than a hundred.
For each one:

1. **Plan** - a cheap model call that turns the archive record into a core
   question, a handful of search phrases and the claims that must hold.
2. **Gather** - retrieval, over the allowlisted institutions only. Wikipedia
   is used as an *index*, not as the answer: its article gives us orientation
   and, more importantly, its external references point at the museums,
   agencies and journals that actually hold the evidence. Those get fetched.
3. **Synthesise** - one model call that turns the gathered text into a
   structured dossier of claims with confidence levels and citations.
4. **Verify** - claim URLs are intersected with what we actually retrieved, in
   code. A citation the model invented cannot survive this step, whatever the
   prompt said.

No paid search API is required. :class:`SearchProvider` is the seam where one
would go, and adding it changes nothing else.
"""

from __future__ import annotations

import abc
import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

from ..config import Config
from ..llm.base import LLMClient, LLMError
from ..llm.prompts import (
    RESEARCH_PLAN_SYSTEM,
    RESEARCH_SYNTHESIS_SYSTEM,
    research_plan_prompt,
    research_synthesis_prompt,
)
from ..logging_setup import Stage, get_logger, stage
from ..models import Candidate, Claim, Disagreement, ResearchDossier, SourceRef
from ..net import FetchError, HttpClient, gather_resilient, is_allowed_url
from ..sanitize import UntrustedContent, any_injection, clean_text, untrusted_block, wrap_untrusted
from .sources import AUTHORITATIVE_THRESHOLD, make_source_ref, rank_sources

logger = get_logger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"

VALID_CONFIDENCE = {"established", "well_evidenced", "plausible", "speculative", "legend"}


@dataclass
class RetrievedPage:
    url: str
    title: str
    text: str
    authority: int


class SearchProvider(abc.ABC):
    """Where research leads come from.

    The default implementation uses Wikipedia's search API, which is free and
    keyless. To use a real web-search API, implement this and pass it to
    :class:`Researcher`.
    """

    @abc.abstractmethod
    async def search(self, query: str, limit: int) -> list[str]:
        """Return candidate URLs for a query."""


class WikipediaSearch(SearchProvider):
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def search(self, query: str, limit: int) -> list[str]:
        try:
            data = await self.http.get_json(WIKI_API, params={
                "action": "query", "format": "json", "formatversion": "2",
                "list": "search", "srsearch": query, "srlimit": str(min(limit, 10)),
                "srnamespace": "0",
            })
        except FetchError as exc:
            logger.debug("wikipedia search failed", query=query, error=str(exc))
            return []
        results = (data.get("query") or {}).get("search") or []
        return [
            f"https://en.wikipedia.org/wiki/{quote(item['title'].replace(' ', '_'))}"
            for item in results if item.get("title")
        ]


@dataclass
class Researcher:
    """Gathers evidence and produces a :class:`ResearchDossier`."""

    config: Config
    http: HttpClient
    llm: LLMClient
    search: SearchProvider | None = None
    _pages: dict[str, RetrievedPage] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.search is None:
            self.search = WikipediaSearch(self.http)

    # ------------------------------------------------------------------ #
    async def research(self, candidate: Candidate) -> ResearchDossier:
        with stage(Stage.RESEARCH):
            logger.info("researching candidate", title=candidate.title[:70],
                        candidate_id=candidate.id)
            errors: list[str] = []

            plan = await self._plan(candidate, errors)
            pages = await self._gather(candidate, plan, errors)
            if not pages:
                dossier = ResearchDossier(
                    core_question=plan.get("core_question", ""),
                    errors=[*errors, "no source material could be retrieved"],
                )
                return dossier

            blocks = [
                wrap_untrusted("source", page.text, source_url=page.url,
                               max_chars=self.config.research.max_page_chars)
                for page in pages
            ]
            injection_flags = any_injection(blocks)
            if injection_flags:
                logger.warning("prompt-injection patterns in retrieved sources",
                               flags=",".join(injection_flags), candidate_id=candidate.id)

            dossier = await self._synthesise(candidate, plan, blocks, pages, errors)
            dossier.pages_read = len(pages)
            dossier.errors = errors
            if injection_flags:
                dossier.content_notes.append(
                    f"Retrieved sources contained model-directed text ({', '.join(injection_flags)}); "
                    "treated as page content."
                )
            logger.info("research complete", candidate_id=candidate.id,
                        claims=len(dossier.claims), sources=len(dossier.sources),
                        pages=len(pages))
            return dossier

    # -- 1. plan -------------------------------------------------------- #
    async def _plan(self, candidate: Candidate, errors: list[str]) -> dict[str, Any]:
        fallback = {
            "core_question": f"What is the story behind “{candidate.title}”?",
            "queries": [candidate.title, *(candidate.entities[:2])],
            "entities": candidate.entities[:6],
            "claims_to_check": [],
            "risks": [],
        }
        try:
            plan = await self.llm.complete_json(
                system=RESEARCH_PLAN_SYSTEM,
                user=research_plan_prompt(candidate),
                purpose="research_plan",
                temperature=self.config.llm.analysis_temperature,
                model=self.config.llm.triage_model,
                max_tokens=1024,
            )
        except LLMError as exc:
            errors.append(f"research plan failed: {exc}")
            logger.warning("research plan failed; using metadata fallback", error=str(exc))
            return fallback
        queries = [q for q in plan.get("queries", []) if isinstance(q, str) and q.strip()]
        plan["queries"] = queries or fallback["queries"]
        plan.setdefault("core_question", fallback["core_question"])
        return plan

    # -- 2. gather ------------------------------------------------------ #
    async def _gather(self, candidate: Candidate, plan: dict[str, Any],
                      errors: list[str]) -> list[RetrievedPage]:
        budget = self.config.research.max_pages_per_candidate
        collected: dict[str, RetrievedPage] = {}

        # (a) The archive's own record for the image is the most direct source.
        seeds: list[str] = []
        for url in (candidate.image.page_url, candidate.source_url):
            if url and url not in seeds:
                seeds.append(url)
        seeds.extend(candidate.raw.get("research_leads", []) or [])

        # (b) Search leads.
        queries = plan.get("queries", [])[:4]
        assert self.search is not None
        search_results = await gather_resilient(
            [self.search.search(query, 3) for query in queries], label="search"
        )
        for batch in search_results:
            seeds.extend(batch)

        wiki_pages = [u for u in seeds if "wikipedia.org/wiki/" in u]
        other_pages = [u for u in seeds if u not in wiki_pages]

        # (c) Fetch the Wikipedia articles first - they are cheap and they
        #     carry the reference lists we actually want.
        for url in wiki_pages[:3]:
            page = await self._fetch_wikipedia(url, errors)
            if page:
                collected[page.url] = page
                other_pages.extend(await self._external_references(url, errors))

        # (d) Then the institutional pages, best authority first.
        ranked = sorted(
            {u for u in other_pages if u},
            key=lambda u: -make_source_ref(u, "").authority,
        )
        remaining = max(budget - len(collected), 0)
        fetched = await gather_resilient(
            [self._fetch_page(url, errors) for url in ranked[: remaining + 3]],
            label="research fetch",
        )
        for page in fetched:
            if page and len(collected) < budget:
                collected.setdefault(page.url, page)

        self._pages = collected
        return list(collected.values())

    async def _fetch_wikipedia(self, url: str, errors: list[str]) -> RetrievedPage | None:
        title = url.rsplit("/wiki/", 1)[-1]
        try:
            data = await self.http.get_json(WIKI_API, params={
                "action": "query", "format": "json", "formatversion": "2",
                "prop": "extracts", "explaintext": "1", "exsectionformat": "plain",
                "titles": title.replace("_", " "), "redirects": "1",
            })
        except FetchError as exc:
            errors.append(f"wikipedia fetch failed ({title}): {exc}")
            return None
        pages = (data.get("query") or {}).get("pages") or []
        if not pages or pages[0].get("missing"):
            return None
        page = pages[0]
        text = clean_text(page.get("extract", ""), max_chars=self.config.research.max_page_chars)
        if len(text) < 200:
            return None
        return RetrievedPage(url=url, title=page.get("title", title), text=text,
                             authority=make_source_ref(url, "").authority)

    async def _external_references(self, wiki_url: str, errors: list[str]) -> list[str]:
        """Pull the article's external links - this is how we reach the
        institutions rather than stopping at the encyclopedia."""
        title = wiki_url.rsplit("/wiki/", 1)[-1].replace("_", " ")
        try:
            data = await self.http.get_json(WIKI_API, params={
                "action": "query", "format": "json", "formatversion": "2",
                "prop": "extlinks", "titles": title, "ellimit": "200", "redirects": "1",
            })
        except FetchError as exc:
            errors.append(f"wikipedia extlinks failed ({title}): {exc}")
            return []
        pages = (data.get("query") or {}).get("pages") or []
        links = []
        for page in pages:
            for entry in page.get("extlinks") or []:
                url = entry.get("url") if isinstance(entry, dict) else entry
                if isinstance(url, str):
                    links.append(url)
        allowed = [
            url for url in links
            if is_allowed_url(url, extra_domains=set(self.config.research.allowed_domains_extra),
                              check_dns=False)[0]
        ]
        # Best sources first; the allowlist has already removed the rest.
        return sorted(set(allowed), key=lambda u: -make_source_ref(u, "").authority)[:8]

    async def _fetch_page(self, url: str, errors: list[str]) -> RetrievedPage | None:
        allowed, reason = is_allowed_url(
            url, extra_domains=set(self.config.research.allowed_domains_extra)
        )
        if not allowed:
            logger.debug("research URL refused", url=url[:120], reason=reason)
            return None
        try:
            body = await asyncio.wait_for(
                self.http.get_text(url, max_bytes=1_500_000),
                timeout=self.config.research.request_timeout_seconds,
            )
        except (FetchError, TimeoutError) as exc:
            errors.append(f"fetch failed ({urlparse(url).hostname}): {exc}")
            return None
        text = clean_text(_readable(body), max_chars=self.config.research.max_page_chars)
        if len(text) < 250:
            return None
        return RetrievedPage(url=url, title=_page_title(body) or url,
                             text=text, authority=make_source_ref(url, "").authority)

    # -- 3. synthesise -------------------------------------------------- #
    async def _synthesise(self, candidate: Candidate, plan: dict[str, Any],
                          blocks: list[UntrustedContent], pages: list[RetrievedPage],
                          errors: list[str]) -> ResearchDossier:
        sources = rank_sources([
            make_source_ref(page.url, page.title, excerpt=page.text[:280]) for page in pages
        ])
        try:
            payload = await self.llm.complete_json(
                system=RESEARCH_SYNTHESIS_SYSTEM,
                user=research_synthesis_prompt(
                    candidate, untrusted_block(blocks), plan.get("core_question", "")
                ),
                purpose="research_synthesis",
                temperature=self.config.llm.analysis_temperature,
                max_tokens=3072,
            )
        except LLMError as exc:
            errors.append(f"research synthesis failed: {exc}")
            logger.warning("synthesis failed", error=str(exc))
            return ResearchDossier(
                core_question=plan.get("core_question", ""),
                summary="", sources=sources, errors=[str(exc)],
            )
        return self._build_dossier(payload, sources, plan)

    def _build_dossier(self, payload: dict[str, Any], sources: list[SourceRef],
                       plan: dict[str, Any]) -> ResearchDossier:
        """Turn the model's JSON into a dossier, discarding anything it made up.

        This is the anti-hallucination step that does not depend on the model
        having behaved: a citation is kept only if we fetched that URL.
        """
        retrieved = {self._normalise(page.url) for page in self._pages.values()}
        source_urls = {self._normalise(s.url) for s in sources}
        permitted = retrieved | source_urls

        claims: list[Claim] = []
        dropped_citations = 0
        for raw in payload.get("claims", []) or []:
            if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
                continue
            supporting = []
            for url in raw.get("supporting_urls", []) or []:
                if not isinstance(url, str):
                    continue
                if self._normalise(url) in permitted:
                    supporting.append(url)
                else:
                    dropped_citations += 1
            confidence = str(raw.get("confidence", "plausible")).lower()
            if confidence not in VALID_CONFIDENCE:
                confidence = "plausible"
            # A claim with no surviving citation cannot be "established".
            if not supporting and confidence in ("established", "well_evidenced"):
                confidence = "plausible"
            claims.append(Claim(
                text=clean_text(str(raw["text"]), max_chars=600),
                confidence=confidence,  # type: ignore[arg-type]
                supporting_urls=supporting,
                contradicting_urls=[
                    u for u in (raw.get("contradicting_urls") or [])
                    if isinstance(u, str) and self._normalise(u) in permitted
                ],
                note=clean_text(str(raw.get("note") or "")) or None,
            ))
        if dropped_citations:
            logger.warning("citations discarded: URL was never retrieved",
                           count=dropped_citations, stage=Stage.FACTCHECK)

        disagreements = [
            Disagreement(
                topic=clean_text(str(item.get("topic", ""))),
                positions=[clean_text(str(p)) for p in (item.get("positions") or [])],
                note=clean_text(str(item.get("note") or "")) or None,
            )
            for item in payload.get("disagreements", []) or []
            if isinstance(item, dict) and item.get("topic")
        ]

        surprising = clean_text(str(payload.get("the_surprising_thing") or ""))
        summary = clean_text(str(payload.get("summary") or ""), max_chars=3000)
        if surprising:
            summary = f"{summary}\n\nMost surprising verified detail: {surprising}"

        return ResearchDossier(
            core_question=clean_text(str(payload.get("core_question")
                                         or plan.get("core_question", ""))),
            summary=summary,
            claims=claims,
            sources=sources,
            disagreements=disagreements,
            entities=[clean_text(str(e)) for e in (payload.get("entities") or [])][:20],
            keywords=[clean_text(str(k)).lower() for k in (payload.get("keywords") or [])][:20],
            timeline=[clean_text(str(t)) for t in (payload.get("timeline") or [])][:20],
            content_notes=[clean_text(str(n)) for n in (payload.get("content_notes") or [])][:8],
        )

    @staticmethod
    def _normalise(url: str) -> str:
        cleaned = url.split("#")[0].rstrip("/").lower()
        return re.sub(r"^https?://(www\.)?", "", cleaned)

    # -- reporting ------------------------------------------------------ #
    @staticmethod
    def sufficiency(dossier: ResearchDossier, config: Config) -> tuple[bool, list[str]]:
        """Is this dossier good enough to write from?"""
        problems: list[str] = []
        research = config.research
        if len(dossier.sources) < research.min_sources_per_edition:
            problems.append(
                f"only {len(dossier.sources)} sources, need "
                f"{research.min_sources_per_edition}"
            )
        if not dossier.claims:
            problems.append("no claims were extracted")
        authoritative = [s for s in dossier.sources if s.authority >= AUTHORITATIVE_THRESHOLD]
        if not authoritative:
            problems.append("no authoritative source (all references are aggregations)")
        key_claims = [c for c in dossier.claims if c.confidence in ("established", "well_evidenced")]
        weak = [
            c for c in key_claims
            if c.independent_support < research.min_independent_sources_for_key_claim
        ]
        if key_claims and len(weak) == len(key_claims):
            problems.append("every load-bearing claim rests on a single source")
        return (not problems), problems


class OfflineResearcher(Researcher):
    """A researcher that reads a bundled dossier instead of the network.

    Used by ``run --offline``, and by the tests. It exists so that the
    writing, quality-control, rendering and delivery stages can be exercised
    end to end on a machine with no outbound access and no API key - which is
    exactly the situation in CI, and on a laptop on a train.

    A candidate with no bundled dossier falls through to the real researcher,
    so mixing offline fixtures with live sources behaves sensibly.
    """

    async def research(self, candidate: Candidate) -> ResearchDossier:
        payload = candidate.raw.get("research")
        if not isinstance(payload, dict):
            return await super().research(candidate)
        with stage(Stage.RESEARCH):
            dossier = ResearchDossier(
                core_question=str(payload.get("core_question", "")),
                summary=str(payload.get("summary", "")),
                claims=[Claim.model_validate(c) for c in payload.get("claims", [])],
                sources=[SourceRef.model_validate(s) for s in payload.get("sources", [])],
                disagreements=[
                    Disagreement.model_validate(d) for d in payload.get("disagreements", [])
                ],
                entities=list(payload.get("entities", [])),
                keywords=list(payload.get("keywords", [])),
                timeline=list(payload.get("timeline", [])),
                content_notes=list(payload.get("content_notes", [])),
                pages_read=0,
            )
            if surprise := payload.get("the_surprising_thing"):
                dossier.summary = (
                    f"{dossier.summary}\n\nMost surprising verified detail: {surprise}"
                )
            logger.info("offline dossier loaded", candidate_id=candidate.id,
                        claims=len(dossier.claims), sources=len(dossier.sources))
            return dossier


_SCRIPTS = re.compile(r"<(script|style|nav|header|footer|aside)\b.*?</\1>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _readable(html: str) -> str:
    """Crudely extract the readable body of an HTML page.

    Deliberately not a full readability implementation - BeautifulSoup and
    friends would be another dependency to buy a marginal improvement over
    "strip the furniture and let the model sort it out".
    """
    from ..sanitize import strip_html

    body = _SCRIPTS.sub(" ", html)
    match = re.search(r"<(?:main|article)\b[^>]*>(.*?)</(?:main|article)>", body, re.I | re.S)
    if match:
        body = match.group(1)
    return strip_html(body)


def _page_title(html: str) -> str:
    from ..sanitize import strip_html

    match = _TITLE.search(html)
    return clean_text(strip_html(match.group(1)))[:200] if match else ""


__all__ = ["OfflineResearcher", "Researcher", "RetrievedPage", "SearchProvider",
           "WikipediaSearch"]
