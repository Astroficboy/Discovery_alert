"""The final gate: what is allowed to leave the building."""

from __future__ import annotations

import pytest

from src.quality.reviewer import QualityReviewer
from tests.conftest import ScriptedLLM

GOOD_REVIEW = {
    "accuracy": 92, "sourcing": 88, "writing": 85, "image_fit": 90,
    "safety_ok": True, "issues": [], "fixes_requested": [],
    "unsupported_claims": [], "notes": "Solid.",
}


def _reviewer(config, review=None) -> QualityReviewer:
    return QualityReviewer(config, ScriptedLLM({"quality": review or GOOD_REVIEW}))


@pytest.mark.asyncio
async def test_a_good_edition_passes(config, candidate, article, dossier):
    report = await _reviewer(config).review(candidate, article, dossier)
    assert report.passed, report.issues


@pytest.mark.asyncio
async def test_a_banned_opening_is_caught(config, candidate, article, dossier):
    bad = article.model_copy(update={
        "hook": "In today's world, abandoned buildings are everywhere."})
    report = await _reviewer(config).review(candidate, bad, dossier)
    assert any("banned construction" in issue for issue in report.issues)
    assert report.writing < 100


@pytest.mark.asyncio
async def test_ai_tics_are_caught(config, candidate, article, dossier):
    bad = article.model_copy(update={
        "story": article.story + "\n\nIt stands as a testament to the rich tapestry of "
                                 "engineering history. It is worth noting this."})
    report = await _reviewer(config).review(candidate, bad, dossier)
    assert any("stock phrasing" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_short_copy_is_rejected(config, candidate, article, dossier):
    short = article.model_copy(update={"story": "Two words.", "bigger_picture": "",
                                       "the_image": ""})
    report = await _reviewer(config).review(candidate, short, dossier)
    assert not report.passed
    assert any("too short" in issue for issue in report.issues)
    assert report.fixes_requested


@pytest.mark.asyncio
async def test_missing_attribution_is_rejected(config, candidate, article, dossier):
    candidate.image.credit = None
    report = await _reviewer(config).review(candidate, article, dossier)
    assert any("credit" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_unusable_licence_zeroes_image_fit(config, candidate, article, dossier):
    candidate.image.license.reusable = False
    report = await _reviewer(config).review(candidate, article, dossier)
    assert not report.passed
    assert report.image_fit == 0.0


@pytest.mark.asyncio
async def test_too_few_sources_is_rejected(config, candidate, article, dossier):
    thin = article.model_copy(update={"sources": article.sources[:1]})
    report = await _reviewer(config).review(candidate, thin, dossier)
    assert not report.passed
    assert any("sources" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_no_authoritative_source_is_rejected(config, candidate, article, dossier):
    from src.models import SourceRef

    weak = article.model_copy(update={"sources": [
        SourceRef(title=f"blog {i}", url=f"https://en.wikipedia.org/wiki/X{i}", authority=50)
        for i in range(4)
    ]})
    report = await _reviewer(config).review(candidate, weak, dossier)
    assert any("authoritative" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_unsupported_prose_claims_drop_accuracy(config, candidate, article, dossier):
    review = {**GOOD_REVIEW,
              "unsupported_claims": ["The station was struck by lightning in 1958."]}
    report = await _reviewer(config, review).review(candidate, article, dossier)
    assert report.accuracy <= 55
    assert not report.passed


@pytest.mark.asyncio
async def test_safety_veto_overrides_good_scores(config, candidate, article, dossier):
    review = {**GOOD_REVIEW, "safety_ok": False}
    report = await _reviewer(config, review).review(candidate, article, dossier)
    assert not report.passed


@pytest.mark.asyncio
async def test_graphic_material_is_flagged(config, candidate, article, dossier):
    grim = article.model_copy(update={
        "story": article.story + "\n\nA close-up of the body was published."})
    report = await _reviewer(config).review(candidate, grim, dossier)
    assert not report.safety_ok


@pytest.mark.asyncio
async def test_content_note_is_required_when_the_subject_demands_it(
        config, candidate, article, dossier):
    heavy = article.model_copy(update={
        "story": article.story + "\n\nThe execution took place at dawn."})
    report = await _reviewer(config).review(candidate, heavy, dossier)
    assert any("content note required" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_repetition_is_detected(config, candidate, article, dossier):
    repetitive = article.model_copy(update={
        "story": "\n\n".join(["The station was quiet. " * 3] * 12)})
    report = await _reviewer(config).review(candidate, repetitive, dossier)
    assert any("repetitive" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_a_failed_reviewer_does_not_wave_an_edition_through(
        config, candidate, article, dossier):
    llm = ScriptedLLM({})
    llm.fail_purposes = {"quality"}
    report = await QualityReviewer(config, llm).review(candidate, article, dossier)
    assert not report.passed
    assert any("could not run" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_markup_in_prose_is_caught(config, candidate, article, dossier):
    marked = article.model_copy(update={
        "story": article.story + "\n\nSee [the record](https://example.com) for more."})
    report = await _reviewer(config).review(candidate, marked, dossier)
    assert any("markdown" in issue.lower() for issue in report.issues)


def test_overall_weights_accuracy_most(config):
    from src.models import QualityReport

    reviewer = QualityReviewer(config, ScriptedLLM())
    accurate = QualityReport(accuracy=100, sourcing=50, writing=50, image_fit=50)
    pretty = QualityReport(accuracy=50, sourcing=50, writing=100, image_fit=50)
    assert reviewer.overall(accurate) > reviewer.overall(pretty)
