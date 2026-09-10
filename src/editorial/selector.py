"""The funnel.

    ~120 discovered        cheap: API calls only
      ->  24 prefiltered   free: heuristics over metadata
      ->   6 triaged       one batched call to a small model
      ->   3 researched    network + one synthesis call each
      ->   2 fully scored  one editorial call each
      ->   1 written       one long call, plus quality review

Each stage is narrower and more expensive than the one above it, which is
what keeps a run in the low tens of cents. The numbers are all in
``config.pipeline``.

The selector deliberately does *not* return "the newest" or "the first good
one". It researches several, scores them against each other, and hands back a
ranked list so the writer stage can fall through to the runner-up if the best
candidate fails quality control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..llm.base import LLMClient, LLMError
from ..llm.prompts import TRIAGE_SYSTEM, triage_prompt
from ..logging_setup import Stage, get_logger, stage
from ..models import Candidate, Edition, TriageVerdict
from ..net import HttpClient
from ..research.fact_checker import FactChecker
from ..research.researcher import Researcher
from ..sanitize import wrap_untrusted
from .deduplicator import Deduplicator
from .rotation import RotationState
from .scorer import Scorer, apply_prefilter

logger = get_logger(__name__)


@dataclass
class SelectionResult:
    """Everything the pipeline needs to know about one selection round."""

    ranked: list[Candidate] = field(default_factory=list)
    discovered: int = 0
    after_prefilter: int = 0
    after_triage: int = 0
    researched: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    fact_checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def best(self) -> Candidate | None:
        return self.ranked[0] if self.ranked else None


@dataclass
class Selector:
    config: Config
    http: HttpClient
    llm: LLMClient
    history: list[Edition]
    #: Injected by ``run --offline`` and by the tests.
    researcher: Researcher | None = None

    def __post_init__(self) -> None:
        self.deduplicator = Deduplicator.from_history(self.config, self.history)
        self.rotation = RotationState.from_history(self.config, self.history)
        if self.researcher is None:
            self.researcher = Researcher(self.config, self.http, self.llm)
        self.fact_checker = FactChecker(self.config, self.llm)
        self.scorer = Scorer(self.config, self.llm, self.rotation)

    @property
    def recent_titles(self) -> list[str]:
        return [edition.title for edition in self.history]

    # ------------------------------------------------------------------ #
    async def select(self, candidates: list[Candidate],
                     source_authority: dict[str, int] | None = None) -> SelectionResult:
        result = SelectionResult(discovered=len(candidates))

        # 1. Free heuristics.
        survivors = apply_prefilter(candidates, self.config, source_authority)

        # 2. Duplicates - before spending anything on them.
        survivors = self.deduplicator.annotate(survivors)
        for candidate in candidates:
            if candidate.rejected_reason:
                result.rejected.append((candidate.title, candidate.rejected_reason))
        result.after_prefilter = len(survivors)
        if not survivors:
            logger.warning("nothing survived prefiltering and deduplication")
            return result

        # 3. One cheap batched triage call.
        survivors = await self._triage(survivors)
        result.after_triage = len(survivors)
        if not survivors:
            return result

        # 4. Research the shortlist.
        shortlist = survivors[: self.config.pipeline.research_keep]
        researched: list[Candidate] = []
        for candidate in shortlist:
            dossier = await self.researcher.research(candidate)
            sufficient, problems = Researcher.sufficiency(dossier, self.config)
            if not sufficient:
                reason = "insufficient research: " + "; ".join(problems)
                candidate.rejected_reason = reason
                result.rejected.append((candidate.title, reason))
                logger.info("candidate dropped after research", title=candidate.title[:60],
                            reason=reason)
                continue
            blocks = [
                wrap_untrusted("source", source.excerpt or "", source_url=source.url)
                for source in dossier.sources if source.excerpt
            ]
            report = await self.fact_checker.check(dossier, blocks)
            result.fact_checks[candidate.id] = report
            candidate.research = dossier
            researched.append(candidate)
        result.researched = len(researched)
        if not researched:
            logger.warning("no candidate produced a usable dossier")
            return result

        # 5. Full editorial scoring on the survivors.
        finalists = researched[: self.config.pipeline.deep_score_keep] or researched
        for candidate in finalists:
            assert candidate.research is not None
            candidate.scores = await self.scorer.score(
                candidate, candidate.research,
                result.fact_checks.get(candidate.id), self.recent_titles,
            )

        with stage(Stage.SELECTION):
            ranked = sorted(
                (c for c in finalists if c.scores),
                key=lambda c: -c.scores.overall,  # type: ignore[union-attr]
            )
            result.ranked = ranked
            if ranked:
                best = ranked[0]
                logger.info("selection complete", winner=best.title[:70],
                            score=best.scores.overall,  # type: ignore[union-attr]
                            runners_up=len(ranked) - 1)
                for candidate in ranked[1:]:
                    logger.debug("runner-up", title=candidate.title[:60],
                                 score=candidate.scores.overall)  # type: ignore[union-attr]
        return result

    # ------------------------------------------------------------------ #
    async def _triage(self, candidates: list[Candidate]) -> list[Candidate]:
        """One call, whole batch. This is the cheapest useful model use in the
        pipeline and it removes most of the remaining noise."""
        with stage(Stage.TRIAGE):
            try:
                verdicts_raw = await self.llm.complete_json(
                    system=TRIAGE_SYSTEM,
                    user=triage_prompt(candidates, self.recent_titles),
                    purpose="triage",
                    temperature=self.config.llm.analysis_temperature,
                    model=self.config.llm.triage_model,
                    max_tokens=3072,
                    expect=list,
                )
            except LLMError as exc:
                logger.warning("triage failed; falling back to prefilter order", error=str(exc))
                return candidates[: self.config.pipeline.triage_keep]

            by_id = {c.id: c for c in candidates}
            scored: list[tuple[float, Candidate]] = []
            for raw in verdicts_raw:
                if not isinstance(raw, dict):
                    continue
                candidate = by_id.get(str(raw.get("candidate_id", "")))
                if candidate is None:
                    continue
                verdict = TriageVerdict(
                    candidate_id=candidate.id,
                    interest=_clamp(raw.get("interest", 0)),
                    the_question=str(raw.get("the_question", ""))[:400],
                    likely_angle=str(raw.get("likely_angle", ""))[:600],
                    domains=[str(d).lower() for d in (raw.get("domains") or [])][:5],
                    verdict=_verdict(raw.get("verdict")),
                    reason=str(raw.get("reason", ""))[:400],
                )
                candidate.triage = verdict
                # The triage model sees more than our keyword taxonomy does;
                # let it add domains, but never remove ours.
                for domain in verdict.domains:
                    if domain in self.config.content.categories and domain not in candidate.categories:
                        candidate.categories.append(domain)
                if verdict.verdict == "drop":
                    candidate.rejected_reason = f"triage: {verdict.reason}"
                    continue
                # Blend triage interest with the free signal so a candidate the
                # model liked but that has a poor image cannot walk straight in.
                blended = 0.7 * verdict.interest + 0.3 * candidate.prefilter_score
                if verdict.verdict == "maybe":
                    blended -= 8
                scored.append((blended, candidate))

            scored.sort(key=lambda pair: -pair[0])
            kept = [candidate for _, candidate in scored[: self.config.pipeline.triage_keep]]
            logger.info("triage complete", considered=len(candidates), kept=len(kept),
                        pursued=sum(1 for _, c in scored
                                    if c.triage and c.triage.verdict == "pursue"))
            return kept


def _clamp(value: Any, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _verdict(value: Any) -> str:
    text = str(value or "maybe").lower().strip()
    return text if text in ("pursue", "maybe", "drop") else "maybe"


__all__ = ["SelectionResult", "Selector"]
