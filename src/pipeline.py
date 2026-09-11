"""The orchestrator.

One object, one method per stage, and a single :meth:`Pipeline.run` that
walks them in order and records what happened. Every stage is individually
runnable from the CLI, which is what makes the thing debuggable at eight in
the morning when an archive has changed its JSON.

Failure policy, stated once here because it governs everything below:

* A dead source, a failed fetch or a bad page costs its own candidate and
  nothing else.
* A failed model call degrades the stage (fall back to mechanical scoring,
  to the prefilter order, to unchecked research) and is recorded.
* A candidate that fails quality control is rewritten once, then abandoned in
  favour of the runner-up.
* If nothing clears the bar, **no email is sent**. The schedule is not a
  reason to publish something mediocre.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import Config
from .delivery.base import EmailConfigurationError, EmailProvider
from .delivery.renderer import RenderedEdition, Renderer
from .delivery.sender import build_provider, send_edition, write_preview
from .discovery import discover_candidates
from .discovery.base import build_sources
from .editorial.selector import SelectionResult, Selector
from .llm.base import LLMClient, LLMError
from .llm.client import build_llm_client
from .llm.writer import Writer
from .logging_setup import Stage, get_logger, new_execution_id, stage
from .models import Article, Candidate, Edition, QualityReport, RunRecord, utcnow
from .net import FetchError, HttpClient, RetryPolicy
from .quality.reviewer import QualityReviewer
from .research.researcher import OfflineResearcher
from .scheduler import Schedule
from .storage.database import Database

logger = get_logger(__name__)


class PipelineError(RuntimeError):
    """A failure that ends the run without sending."""


@dataclass
class PipelineOutcome:
    """Everything a caller (CLI, tests, a future web UI) needs afterwards."""

    run: RunRecord
    candidate: Candidate | None = None
    article: Article | None = None
    quality: QualityReport | None = None
    rendered: RenderedEdition | None = None
    edition: Edition | None = None
    sent: bool = False
    preview_path: Path | None = None
    selection: SelectionResult | None = None
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.run.outcome in ("sent", "dry_run", "awaiting_review", "not_due")


@dataclass
class Pipeline:
    config: Config
    database: Database
    dry_run: bool = False
    review: bool = False
    force: bool = False
    #: Offline mode: bundled candidates and bundled dossiers, no outbound
    #: network beyond verifying the hero image (which is also skipped).
    offline: bool = False
    #: When False, a draft that fails quality control is published anyway.
    #: This exists to smoke-test the plumbing end to end - discovery through
    #: delivery - without waiting on a story good enough to earn a send. It is
    #: not a tuning knob: lower `content.min_quality_score` for that. Every
    #: edition published this way is stamped in the footer, recorded in the run
    #: log, and announced loudly, so it can never be mistaken for a real one.
    quality_gate: bool = True
    llm: LLMClient | None = None
    email_provider: EmailProvider | None = None
    _owns_llm: bool = field(default=False, init=False)
    #: The best draft that failed quality control, kept so that a dry run can
    #: still show you what it produced and why it was refused.
    _best_rejected: tuple[Candidate, Article, QualityReport] | None = field(
        default=None, init=False, repr=False
    )

    # ------------------------------------------------------------------ #
    async def run(self) -> PipelineOutcome:
        execution_id = new_execution_id()
        mode = "dry_run" if self.dry_run else ("review" if self.review else "run")
        run = RunRecord(execution_id=execution_id, mode=mode)
        self.database.start_run(run)
        outcome = PipelineOutcome(run=run)

        try:
            schedule = Schedule(self.config)
            decision = schedule.decide(self.database, force=self.force or self.dry_run)
            run.stages["schedule"] = decision.reason
            if not decision.should_send:
                run.outcome = "not_due"
                outcome.skipped_reason = decision.reason
                logger.info("nothing to do", reason=decision.reason)
                return outcome

            await self._run_stages(run, outcome, decision.edition_date)
        except PipelineError as exc:
            run.outcome = "failed"
            run.errors.append(str(exc))
            logger.error("pipeline failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - the run record must always close
            run.outcome = "failed"
            run.errors.append(f"{type(exc).__name__}: {exc}")
            logger.error("pipeline crashed", error=f"{type(exc).__name__}: {exc}",
                         exc_info=True)
        finally:
            run.finished_at = utcnow()
            if self.llm is not None:
                run.stages["llm_usage"] = {
                    "calls": self.llm.usage.calls,
                    "input_tokens": self.llm.usage.input_tokens,
                    "output_tokens": self.llm.usage.output_tokens,
                    "by_purpose": self.llm.usage.by_purpose,
                }
            self.database.finish_run(run)
            await self._close()
        return outcome

    # ------------------------------------------------------------------ #
    async def _run_stages(self, run: RunRecord, outcome: PipelineOutcome,
                          edition_date: date) -> None:
        if self.llm is None:
            self.llm = build_llm_client(self.config)
            self._owns_llm = True

        history = self.database.recent_editions(limit=120)
        if self.offline:
            logger.warning("offline mode: using bundled candidates and bundled research")

        async with HttpClient(
            user_agent=self.config.user_agent,
            timeout=float(self.config.research.request_timeout_seconds),
            retry=RetryPolicy(attempts=3),
        ) as http:
            # 1-2. Discovery
            candidates = (self._offline_candidates(http) if self.offline
                          else await discover_candidates(self.config, http))
            run.discovered = len(candidates)
            if not candidates:
                raise PipelineError(
                    "no candidates were discovered - every source failed or returned nothing"
                )

            authority = {
                source.name: source.authority for source in build_sources(self.config, http)
            }

            # 3-6. Selection funnel
            researcher = OfflineResearcher(self.config, http, self.llm) if self.offline else None
            selector = Selector(self.config, http, self.llm, history, researcher=researcher)
            selection = await selector.select(candidates, authority)
            outcome.selection = selection
            run.after_prefilter = selection.after_prefilter
            run.after_triage = selection.after_triage
            run.researched = selection.researched
            run.stages["rejected"] = [
                {"title": title[:120], "reason": reason[:200]}
                for title, reason in selection.rejected[:20]
            ]
            if not selection.ranked:
                self._skip(run, outcome, "no candidate survived research and scoring")
                return

            # 7-8. Write and review, falling through to the runner-up.
            written = await self._write_best(selection, run)
            if written is None:
                self._skip(run, outcome,
                           "no candidate met the quality threshold "
                           f"({self.config.content.min_quality_score})")
                self._write_rejected_preview(outcome, edition_date)
                return
            candidate, article, quality = written
            outcome.candidate, outcome.article, outcome.quality = candidate, article, quality
            run.selected_candidate_id = candidate.id
            run.selected_title = article.title
            run.selected_score = candidate.scores.overall if candidate.scores else None

            # 9. Confirm the hero image is really there before we email it.
            if self.offline:
                logger.info("offline mode: skipping hero image verification",
                            stage=Stage.IMAGE)
            else:
                await self._verify_image(candidate, http, run)

            # 10. Render.
            issue_number = self.database.next_issue_number(
                self.config.newsletter.first_issue_number
            )
            renderer = Renderer(self.config)
            rendered = renderer.render(candidate, article, issue_number=issue_number,
                                       edition_date=edition_date,
                                       footer_note=self._footer_note(candidate, quality))
            outcome.rendered = rendered
            run.issue_number = issue_number

            outcome.preview_path = write_preview(rendered, self.config.output_dir)
            self._write_run_artifacts(rendered, candidate, article, quality)

            edition = self._edition_record(candidate, article, issue_number, edition_date,
                                           outcome.preview_path)
            outcome.edition = edition

            # 11. Send, unless we are not supposed to.
            if self.dry_run:
                run.outcome = "dry_run"
                run.email_status = "skipped (dry run)"
                logger.info("dry run complete; nothing was sent")
                return
            if self.review:
                edition.status = "pending_review"
                self.database.record_edition(edition, article_json=_article_json(article))
                run.outcome = "awaiting_review"
                run.email_status = "awaiting approval"
                logger.info("edition held for review",
                            issue=issue_number, path=str(outcome.preview_path))
                return

            self.database.record_edition(edition, article_json=_article_json(article))
            result = await send_edition(self.config, rendered, provider=self.email_provider)
            run.email_status = f"{result.provider}: {result.detail}"[:300]
            if result.ok:
                self.database.mark_edition_sent(issue_number)
                self.database.remember_candidate(candidate, "sent")
                run.outcome = "sent"
                outcome.sent = True
                logger.info("edition sent", issue=issue_number, title=article.title[:70],
                            recipients=len(result.recipients))
            else:
                self.database.set_edition_status(issue_number, "failed")
                run.outcome = "send_failed"
                run.errors.append(f"email send failed: {result.detail}")
                raise PipelineError(f"email send failed: {result.detail}")

    # ------------------------------------------------------------------ #
    async def _write_best(self, selection: SelectionResult,
                          run: RunRecord) -> tuple[Candidate, Article, QualityReport] | None:
        """Write, review, and fall through to the next candidate on failure."""
        writer = Writer(self.config, self.llm)  # type: ignore[arg-type]
        reviewer = QualityReviewer(self.config, self.llm)  # type: ignore[arg-type]
        threshold = self.config.content.min_quality_score
        attempts_left = self.config.pipeline.write_attempts
        report_log: list[dict[str, Any]] = []

        for candidate in selection.ranked:
            if attempts_left <= 0:
                break
            attempts_left -= 1
            dossier = candidate.research
            if dossier is None:  # pragma: no cover - selector guarantees this
                continue
            fact_check = selection.fact_checks.get(candidate.id)

            revision_notes: list[str] | None = None
            for rewrite in range(self.config.quality.max_regeneration_attempts + 1):
                try:
                    article = await writer.write(candidate, dossier, fact_check, revision_notes)
                except LLMError as exc:
                    logger.warning("writing failed", candidate_id=candidate.id, error=str(exc))
                    run.errors.append(f"writing failed for {candidate.title[:60]}: {exc}")
                    break

                quality = await reviewer.review(candidate, article, dossier)
                score = reviewer.overall(quality)
                combined = self._combined_score(candidate, score)
                report_log.append({
                    "candidate": candidate.title[:120],
                    "attempt": rewrite + 1,
                    "quality": score,
                    "editorial": candidate.scores.overall if candidate.scores else None,
                    "combined": combined,
                    "passed": quality.passed,
                    "issues": quality.issues[:6],
                })
                if (self._best_rejected is None
                        or combined > self._combined_score(
                            self._best_rejected[0],
                            reviewer.overall(self._best_rejected[2]))):
                    self._best_rejected = (candidate, article, quality)

                if quality.passed and combined >= threshold:
                    candidate.article = article
                    candidate.quality = quality
                    run.stages["quality"] = report_log
                    logger.info("edition cleared quality control",
                                title=article.title[:70], quality=score, combined=combined)
                    return candidate, article, quality

                reason = (f"quality {score} / combined {combined} below "
                          f"{threshold}: {'; '.join(quality.issues[:3])}")

                if not self.quality_gate:
                    candidate.article = article
                    candidate.quality = quality
                    run.stages["quality"] = report_log
                    run.stages["quality_gate_bypassed"] = reason
                    logger.warning(
                        "QUALITY GATE BYPASSED - publishing a draft that failed review",
                        title=article.title[:70], quality=score, combined=combined,
                        blocking=len(quality.blocking_issues),
                    )
                    for issue in quality.issues[:6]:
                        logger.warning("bypassed issue", issue=issue[:200])
                    return candidate, article, quality

                logger.warning("draft rejected", candidate_id=candidate.id, reason=reason[:280])
                if rewrite < self.config.quality.max_regeneration_attempts:
                    revision_notes = (quality.fixes_requested or quality.issues)[:6]
                    logger.info("requesting one rewrite", fixes=len(revision_notes))
                else:
                    self.database.remember_candidate(candidate, "rejected", reason[:400])

        run.stages["quality"] = report_log
        return None

    def _combined_score(self, candidate: Candidate, quality_score: float) -> float:
        """The editorial score says the story is worth telling; the quality
        score says it was told well. An edition needs both."""
        editorial = candidate.scores.overall if candidate.scores else 0.0
        return round(0.55 * editorial + 0.45 * quality_score, 2)

    async def _verify_image(self, candidate: Candidate, http: HttpClient,
                            run: RunRecord) -> None:
        """Confirm the hero image resolves. A broken image is a broken edition,
        and it is the one thing we cannot apologise for afterwards."""
        with stage(Stage.IMAGE):
            url = candidate.image.url
            try:
                response = await http.head_or_get(url)
            except FetchError as exc:
                run.errors.append(f"hero image could not be verified: {exc}")
                logger.error("hero image unreachable", url=url[:120], error=str(exc))
                raise PipelineError(
                    f"the hero image could not be fetched ({exc}); refusing to send an "
                    "edition with a broken image"
                ) from exc
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type and not content_type.startswith("image/"):
                raise PipelineError(
                    f"hero image URL returned {content_type or 'no content type'}, not an image"
                )
            if content_type and content_type not in self.config.image.allowed_mime:
                logger.warning("hero image type is unusual for email",
                               content_type=content_type)
            candidate.image.mime_type = candidate.image.mime_type or content_type or None
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                candidate.image.size_bytes = int(length)
                if int(length) > self.config.image.max_download_bytes:
                    logger.warning("hero image is very large; some clients will not load it",
                                   bytes=int(length))
            logger.info("hero image verified", content_type=content_type or "unknown",
                        bytes=candidate.image.size_bytes or 0)

    # ------------------------------------------------------------------ #
    def _write_rejected_preview(self, outcome: PipelineOutcome, edition_date: date) -> None:
        """A dry run that produced nothing sendable still owes you the draft.

        Written to ``output/rejected-newsletter.html`` - a deliberately
        different filename, so it can never be mistaken for a real edition or
        picked up by anything expecting one.
        """
        if not self.dry_run or self._best_rejected is None:
            return
        candidate, article, quality = self._best_rejected
        try:
            rendered = Renderer(self.config).render(
                candidate, article,
                issue_number=self.database.next_issue_number(
                    self.config.newsletter.first_issue_number
                ),
                edition_date=edition_date,
                footer_note="REJECTED DRAFT - this edition did not pass quality control",
            )
        except Exception as exc:  # noqa: BLE001 - a preview is a nicety
            logger.warning("could not render the rejected draft", error=str(exc))
            return
        output = self.config.output_dir
        output.mkdir(parents=True, exist_ok=True)
        path = output / "rejected-newsletter.html"
        path.write_text(rendered.html, encoding="utf-8")
        outcome.candidate, outcome.article, outcome.quality = candidate, article, quality
        outcome.rendered = rendered
        outcome.preview_path = path
        self._write_run_artifacts(rendered, candidate, article, quality)
        logger.info("rejected draft written for inspection", path=str(path))

    def _offline_candidates(self, http: HttpClient) -> list[Candidate]:
        """The bundled fixtures, unconditionally, with the licence gate still
        applied - offline is a shortcut around the network, not around policy."""
        from .config import SourceConfig
        from .discovery.fixtures import BundledFixtures

        configured = next(
            (s for s in self.config.discovery.sources if s.name == "fixtures"), None
        )
        source_config = configured or SourceConfig(name="fixtures", limit=20)
        source = BundledFixtures(self.config, source_config, http)
        candidates = source.apply_gates(source.load())
        logger.info("offline candidates loaded", count=len(candidates), stage=Stage.DISCOVERY)
        return candidates

    def _skip(self, run: RunRecord, outcome: PipelineOutcome, reason: str) -> None:
        if self.config.pipeline.skip_edition_if_below_threshold:
            run.outcome = "skipped"
            run.email_status = "skipped"
            outcome.skipped_reason = reason
            logger.warning("no edition today", reason=reason)
        else:  # pragma: no cover - opt-in behaviour
            run.outcome = "failed"
            run.errors.append(reason)

    def _footer_note(self, candidate: Candidate, quality: QualityReport) -> str | None:
        bits = []
        if candidate.scores:
            bits.append(f"Editorial score {candidate.scores.overall:.0f}")
        if self.config.llm.provider == "stub":
            bits.append("generated without a language model")
        if not quality.passed:
            # Only reachable with the gate bypassed. Say so on the edition
            # itself, so a test send is unmistakable in the inbox.
            bits.append("QUALITY GATE BYPASSED - this edition did not pass review")
        return " · ".join(bits) if bits else None

    def _edition_record(self, candidate: Candidate, article: Article, issue_number: int,
                        edition_date: date, preview_path: Path | None) -> Edition:
        return Edition(
            issue_number=issue_number,
            edition_date=edition_date,
            candidate_id=candidate.id,
            title=article.title,
            category=(candidate.categories or ["uncategorised"])[0],
            categories=candidate.categories,
            image_url=candidate.image.url,
            image_page_url=candidate.image.page_url,
            image_credit=candidate.image.credit,
            score=candidate.scores.overall if candidate.scores else 0.0,
            music=candidate.music,
            keywords=(candidate.research.keywords if candidate.research else [])
            or candidate.keywords,
            entities=(candidate.research.entities if candidate.research else [])
            or candidate.entities,
            source_urls=[s.url for s in article.sources],
            html_path=str(preview_path) if preview_path else None,
            status="draft",
        )

    def _write_run_artifacts(self, rendered: RenderedEdition, candidate: Candidate,
                             article: Article, quality: QualityReport) -> None:
        """Drop a machine-readable record of the run next to the HTML."""
        output = self.config.output_dir
        output.mkdir(parents=True, exist_ok=True)
        payload = {
            "issue_number": rendered.issue_number,
            "edition_date": rendered.edition_date.isoformat(),
            "subject": rendered.subject,
            "candidate": {
                "id": candidate.id,
                "source": candidate.source,
                "title": candidate.title,
                "categories": candidate.categories,
                "music": candidate.music.model_dump(exclude_none=True)
                if candidate.music else None,
                "image": candidate.image.model_dump(),
                "scores": candidate.scores.model_dump() if candidate.scores else None,
            },
            "article": article.model_dump(),
            "quality": quality.model_dump(),
            "research": candidate.research.model_dump() if candidate.research else None,
        }
        (output / "edition.json").write_text(
            json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
        )

    async def _close(self) -> None:
        if self.llm is not None and self._owns_llm:
            await self.llm.aclose()
        if self.email_provider is not None:
            await self.email_provider.aclose()


def _article_json(article: Article) -> str:
    return json.dumps(article.model_dump(), default=str, ensure_ascii=False)


def build_pipeline(config: Config, *, dry_run: bool = False, review: bool = False,
                   force: bool = False, offline: bool = False,
                   quality_gate: bool = True,
                   database: Database | None = None) -> Pipeline:
    database = database or Database(config.database_file)
    if dry_run:
        # A dry run must be structurally incapable of sending. Swapping the
        # provider is stronger than remembering not to call send().
        try:
            provider: EmailProvider | None = build_provider(config, force="file")
        except EmailConfigurationError:  # pragma: no cover - FileProvider cannot fail
            provider = None
    else:
        provider = None
    return Pipeline(config=config, database=database, dry_run=dry_run, review=review,
                    force=force, offline=offline, quality_gate=quality_gate,
                    email_provider=provider)


__all__ = ["Pipeline", "PipelineError", "PipelineOutcome", "build_pipeline"]
