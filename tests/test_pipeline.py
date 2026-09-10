"""End-to-end: the funnel, the failure paths, and the promises the CLI makes."""

from __future__ import annotations

from datetime import date

import pytest

from src.delivery.providers.console import ConsoleProvider
from src.editorial.selector import Selector
from src.models import Edition
from src.pipeline import Pipeline, build_pipeline
from src.research.researcher import OfflineResearcher
from tests.conftest import ScriptedLLM, fixture_candidates, fixture_ids, mock_http

# A model that behaves well at every stage. The draft below is deliberately
# realistic: inside the word band, varied sentence openings, no stock phrasing -
# so that a test failure means the pipeline changed, not that the fake copy did.
_PARAGRAPHS = [
    ("The relay station sits four kilometres from the nearest road, on a ridge "
     "chosen in 1947 because it had a clear line of sight to two other ridges. "
     "Getting equipment up there took a winter. Three men lived in a hut beside "
     "the mast while they built it, and one of them kept a diary that survives in "
     "the county record office. Most of it is about the weather."),
    ("Traffic through the station peaked in 1953 and then declined for eight "
     "years as the network it belonged to was gradually rerouted through cable. "
     "By 1961 the mast was carrying almost nothing. The closure order, when it "
     "came, gave the last operator four days' notice and no instructions about "
     "what to do with the building."),
    ("So he did what the order said and nothing more. He filed the final log, "
     "switched the transmitter to standby, walked down the track and did not come "
     "back. The door was never locked because locking it was not on the list. "
     "Nobody was assigned the job of returning, and the land belonged to a company "
     "that had already stopped existing."),
    ("Surveyors found it again in 1974, working on an unrelated boundary dispute. "
     "Their photographs show the racks still in place, the log book open on the "
     "desk, and a mug. Local walkers had been using the building as a shelter for "
     "over a decade without knowing what it was. The county council listed it in "
     "1998, largely on the strength of how completely it had been left alone."),
    ("Preservation was never the intention. What kept the station intact was a "
     "gap between two administrative systems - one that decommissioned equipment "
     "and one that disposed of buildings - and the fact that neither had a line "
     "for a concrete shed on a ridge nobody drove past."),
]


def _replies(candidate_ids: list[str]) -> dict:
    story = "\n\n".join(_PARAGRAPHS)
    return {
        "triage": [
            {"candidate_id": cid, "interest": 88 - index, "the_question": "What is this?",
             "likely_angle": "The story behind it.", "domains": ["history"],
             "verdict": "pursue", "reason": "Strong image, real story."}
            for index, cid in enumerate(candidate_ids)
        ],
        "fact_check": {"verified": [], "unsupported": [], "corrections": [],
                       "downgrade_confidence": [], "single_source_claims": [],
                       "internal_contradictions": [], "notes": ""},
        "score": {
            "visual_score": 88, "story_score": 90, "novelty_score": 86,
            "significance_score": 84, "curiosity_score": 92,
            "historical_significance": 85, "scientific_significance": 40,
            "emotional_impact": 70, "music_significance": 60,
            "cultural_significance": 75, "technical_significance": 70,
            "genre_interest": 65, "rationale": "A genuinely surprising object.",
            "concerns": [],
        },
        "write": {
            "title": "The relay station nobody bothered to close",
            "subtitle": "It was left open in 1961, and it is still open",
            "hook": "The door has been open since 1961. Nobody locked it, because "
                    "nobody knew they were the last person out.",
            "the_image": "You are looking at a concrete shed on a ridge, photographed "
                         "in 1961 by a survey team that was not looking for it.",
            "story": story,
            "bigger_picture": (
                "What the station did mattered less, in the end, than what happened "
                "to it afterwards. The network it belonged to was replaced twice "
                "over before anyone thought to ask what had become of its physical "
                "plant, and by then most of it was gone. This one survived because "
                "it fell through a gap in the paperwork.\n\nThat gap turns out to "
                "be the reason we know anything about how these stations were "
                "actually operated. Every other site in the chain was stripped, "
                "sold or demolished, and the operating manuals went with them."
            ),
            "one_more_thing": "The last logged transmission was a weather report for a "
                              "ship that had already docked.",
            "listen": None, "content_note": None,
        },
        "quality": {"accuracy": 93, "sourcing": 90, "writing": 88, "image_fit": 92,
                    "safety_ok": True, "issues": [], "fixes_requested": [],
                    "unsupported_claims": [], "notes": "Ready."},
    }


@pytest.fixture
def offline_pipeline(config, database, monkeypatch):
    """A pipeline wired to bundled candidates, bundled research and a good model."""
    def _build(replies_for=None, **kwargs):
        llm = ScriptedLLM(replies_for or _replies(fixture_ids(config)))
        pipeline = Pipeline(config=config, database=database, offline=True,
                            llm=llm, **kwargs)
        return pipeline, llm

    return _build


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dry_run_produces_a_newsletter_and_sends_nothing(offline_pipeline, config,
                                                               tmp_path):
    pipeline, llm = offline_pipeline(dry_run=True, force=True,
                                     email_provider=ConsoleProvider())
    outcome = await pipeline.run()

    assert outcome.run.outcome == "dry_run"
    assert outcome.sent is False
    assert outcome.article is not None
    assert outcome.quality is not None and outcome.quality.passed
    assert outcome.preview_path is not None and outcome.preview_path.exists()
    assert (config.output_dir / "edition.json").exists()
    assert "newsletter.html" == outcome.preview_path.name
    html = outcome.preview_path.read_text()
    assert outcome.article.title in html
    assert "Sources &amp; further reading" in html


@pytest.mark.asyncio
async def test_a_real_run_sends_and_records_the_edition(offline_pipeline, config, database,
                                                        tmp_path):
    pipeline, _ = offline_pipeline(force=True, email_provider=ConsoleProvider())
    outcome = await pipeline.run()

    assert outcome.run.outcome == "sent"
    assert outcome.sent is True
    stored = database.get_edition(outcome.run.issue_number)
    assert stored is not None and stored.status == "sent"
    assert stored.image_credit
    assert stored.source_urls


@pytest.mark.asyncio
async def test_review_mode_holds_the_edition(offline_pipeline, config, database, tmp_path):
    pipeline, _ = offline_pipeline(review=True, force=True,
                                   email_provider=ConsoleProvider())
    outcome = await pipeline.run()

    assert outcome.run.outcome == "awaiting_review"
    assert outcome.sent is False
    stored = database.get_edition(outcome.run.issue_number)
    assert stored.status == "pending_review"
    assert database.get_edition_article(outcome.run.issue_number) is not None


@pytest.mark.asyncio
async def test_the_same_edition_is_not_sent_twice(offline_pipeline, config, database,
                                                  tmp_path):
    first, _ = offline_pipeline(force=True, email_provider=ConsoleProvider())
    assert (await first.run()).run.outcome == "sent"

    # A second run on the same day, without --force, must decline.
    second, _ = offline_pipeline(email_provider=ConsoleProvider())
    outcome = await second.run()
    assert outcome.run.outcome == "not_due"
    assert "already exists" in outcome.skipped_reason


@pytest.mark.asyncio
async def test_a_repeat_subject_is_refused(offline_pipeline, config, database, tmp_path):
    """The dedup layer must actually be wired into the pipeline."""
    pipeline, _ = offline_pipeline(force=True, email_provider=ConsoleProvider())
    first = await pipeline.run()
    assert first.candidate is not None
    used_id = first.candidate.id

    again, _ = offline_pipeline(force=True, email_provider=ConsoleProvider())
    second = await again.run()
    if second.candidate is not None:
        assert second.candidate.id != used_id


# --------------------------------------------------------------------------- #
# Refusing to send
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_failing_draft_is_never_sent(offline_pipeline, config, database, tmp_path):
    ids_replies = _replies(fixture_ids(config))
    bad = {**ids_replies,
           "quality": {"accuracy": 30, "sourcing": 30, "writing": 20, "image_fit": 40,
                       "safety_ok": True, "issues": ["invented a date"],
                       "fixes_requested": ["remove the invented date"],
                       "unsupported_claims": ["The station burned down in 1970."],
                       "notes": "Not publishable."}}
    pipeline, _ = offline_pipeline(bad, force=True, email_provider=ConsoleProvider())
    outcome = await pipeline.run()

    assert outcome.run.outcome == "skipped"
    assert outcome.sent is False
    assert "quality threshold" in outcome.skipped_reason
    assert database.last_sent_edition() is None


@pytest.mark.asyncio
async def test_the_stub_writer_cannot_pass_the_gate(config, database, tmp_path):
    """A newsletter of placeholder prose is worse than no newsletter."""
    config.llm.provider = "stub"
    config.llm_fell_back_to_stub = True
    pipeline = build_pipeline(config, dry_run=True, force=True, offline=True,
                              database=database)
    outcome = await pipeline.run()
    assert outcome.sent is False
    assert outcome.run.outcome == "skipped"


@pytest.mark.asyncio
async def test_a_rejected_draft_is_still_written_for_inspection(config, database, tmp_path):
    config.llm.provider = "stub"
    pipeline = build_pipeline(config, dry_run=True, force=True, offline=True,
                              database=database)
    outcome = await pipeline.run()
    assert outcome.preview_path is not None
    assert outcome.preview_path.name == "rejected-newsletter.html"
    assert "REJECTED DRAFT" in outcome.preview_path.read_text()


@pytest.mark.asyncio
async def test_a_send_failure_is_reported(offline_pipeline, config, database, tmp_path):
    from src.delivery.base import EmailMessage, EmailProvider, SendResult

    class _Broken(EmailProvider):
        name = "broken"

        async def send(self, message: EmailMessage) -> SendResult:
            return SendResult(False, self.name, detail="550 mailbox unavailable")

    pipeline, _ = offline_pipeline(force=True, email_provider=_Broken())
    outcome = await pipeline.run()

    assert outcome.run.outcome == "failed"
    assert outcome.sent is False
    assert any("send failed" in error for error in outcome.run.errors)
    stored = database.get_edition(outcome.run.issue_number)
    assert stored.status == "failed"


@pytest.mark.asyncio
async def test_nothing_discovered_fails_cleanly(config, database, tmp_path):
    for source in config.discovery.sources:
        source.enabled = False
    pipeline = Pipeline(config=config, database=database, dry_run=True, force=True,
                        llm=ScriptedLLM(), email_provider=ConsoleProvider())
    outcome = await pipeline.run()
    assert outcome.run.outcome == "failed"
    assert outcome.sent is False


@pytest.mark.asyncio
async def test_off_schedule_run_does_nothing(config, database, tmp_path, monkeypatch):
    config.newsletter.frequency_days = 2
    # Anchor the epoch so that today is definitely an off day.
    config.newsletter.epoch_date = (date.today().replace(day=1)).isoformat()
    from src.scheduler import Schedule

    if Schedule(config).is_send_day(date.today()):
        config.newsletter.epoch_date = date.fromordinal(
            date.today().toordinal() - 1
        ).isoformat()
    pipeline = Pipeline(config=config, database=database, llm=ScriptedLLM(),
                        email_provider=ConsoleProvider(), offline=True)
    outcome = await pipeline.run()
    assert outcome.run.outcome == "not_due"


# --------------------------------------------------------------------------- #
# The funnel narrows
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_expensive_stages_see_far_fewer_candidates(config, database):
    """The cost design, asserted: research must not run on everything."""
    config.pipeline.triage_keep = 2
    config.pipeline.research_keep = 1
    config.pipeline.deep_score_keep = 1

    candidates = fixture_candidates(config)
    async with mock_http({}) as http:
        llm = ScriptedLLM(_replies([c.id for c in candidates]))
        selector = Selector(config, http, llm, [],
                            researcher=OfflineResearcher(config, http, llm))
        result = await selector.select(candidates)

    assert result.after_prefilter >= result.after_triage
    assert result.after_triage <= 2
    assert result.researched <= 1
    triage_calls = [c for c in llm.calls if c[0] == "triage"]
    assert len(triage_calls) == 1, "triage must be one batched call, not one per candidate"


@pytest.mark.asyncio
async def test_selection_prefers_the_higher_scoring_candidate(config):
    candidates = fixture_candidates(config)
    async with mock_http({}) as http:
        llm = ScriptedLLM(_replies([c.id for c in candidates]))
        selector = Selector(config, http, llm, [],
                            researcher=OfflineResearcher(config, http, llm))
        result = await selector.select(candidates)

    scores = [c.scores.overall for c in result.ranked]
    assert scores == sorted(scores, reverse=True)
    assert result.best is result.ranked[0]


@pytest.mark.asyncio
async def test_previously_sent_candidates_are_excluded_by_the_selector(config):
    candidates = fixture_candidates(config)
    async with mock_http({}) as http:
        history = [Edition(
            issue_number=1, edition_date=date.today(), candidate_id=candidates[0].id,
            title=candidates[0].title, category="space", categories=["space"],
            image_url=candidates[0].image.url,
        )]
        llm = ScriptedLLM(_replies([c.id for c in candidates]))
        selector = Selector(config, http, llm, history,
                            researcher=OfflineResearcher(config, http, llm))
        result = await selector.select(candidates)

    assert all(c.id != candidates[0].id for c in result.ranked)
