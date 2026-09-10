"""Deduplication, rotation and scoring."""

from __future__ import annotations

from datetime import date, timedelta

from src.editorial.deduplicator import Deduplicator, jaccard, overlap_coefficient, tokens
from src.editorial.rotation import RotationState, cross_domain_bonus, rotation_bonus
from src.editorial.scorer import apply_prefilter, prefilter_score
from src.editorial.taxonomy import classify_domains, detect_music, era_from_text
from src.models import Candidate, Edition, ImageAsset, LicenseInfo, MusicMeta, ScoreCard


def _edition(**kwargs) -> Edition:
    defaults = dict(
        issue_number=1, edition_date=date.today() - timedelta(days=4), candidate_id="prev",
        title="A previous edition", category="space", categories=["space"],
        image_url="https://archive.test/a/b/Previous.jpg", keywords=[], entities=[],
    )
    return Edition(**{**defaults, **kwargs})


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #
def test_same_candidate_id_is_a_duplicate(config, candidate):
    previous = _edition(candidate_id=candidate.id)
    verdict = Deduplicator.from_history(config, [previous]).check(candidate)
    assert verdict.is_duplicate
    assert "already been sent" in verdict.reason


def test_same_image_at_a_different_size_is_a_duplicate(config, candidate):
    previous = _edition(
        image_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Example.jpg/800px-Example.jpg"
    )
    verdict = Deduplicator.from_history(config, [previous]).check(candidate)
    assert verdict.is_duplicate
    assert "image has already been sent" in verdict.reason


def test_entity_cooldown_blocks_the_apollo_case(config, image):
    """The brief's own example: an Apollo story must not follow an Apollo story."""
    previous = _edition(title="Apollo 12 visits Surveyor 3", categories=["space"],
                        entities=["Apollo", "Conrad"], keywords=["apollo", "surveyor"])
    candidate = Candidate(source="s", source_url="u", title="Apollo 11's lunar module",
                          image=image, categories=["space"],
                          entities=["Apollo", "Armstrong"], keywords=["apollo", "lunar"])
    verdict = Deduplicator.from_history(config, [previous]).check(candidate)
    assert verdict.is_duplicate
    assert "cooldown" in verdict.reason


def test_a_related_topic_returns_after_the_cooldown(config, image):
    old = _edition(edition_date=date.today() - timedelta(days=400),
                   title="Apollo 12 visits Surveyor 3", entities=["Apollo"],
                   keywords=["apollo"])
    candidate = Candidate(source="s", source_url="u", title="Apollo 11's lunar module",
                          image=image, categories=["space"], entities=["Apollo"],
                          keywords=["apollo", "lunar", "module", "eagle"])
    verdict = Deduplicator.from_history(config, [old]).check(candidate)
    assert not verdict.is_duplicate


def test_shared_category_alone_is_not_a_duplicate(config, image):
    previous = _edition(title="A nebula in Orion", categories=["space"],
                        entities=["Orion"], keywords=["nebula", "orion", "dust"])
    candidate = Candidate(source="s", source_url="u", title="A Soviet lunar rover",
                          image=image, categories=["space"], entities=["Lunokhod"],
                          keywords=["rover", "lunokhod", "soviet"])
    verdict = Deduplicator.from_history(config, [previous]).check(candidate)
    assert not verdict.is_duplicate
    assert verdict.similarity < 0.3


def test_mild_overlap_is_penalised_not_blocked(config, image):
    previous = _edition(title="A Soviet lunar rover", categories=["space", "engineering"],
                        entities=["Lunokhod"], keywords=["rover", "soviet", "lunar"])
    candidate = Candidate(source="s", source_url="u", title="A Soviet Venus lander",
                          image=image, categories=["space", "engineering"],
                          entities=["Venera"], keywords=["lander", "soviet", "venus"])
    verdict = Deduplicator.from_history(config, [previous]).check(candidate)
    assert not verdict.is_duplicate
    assert verdict.penalty > 0


def test_annotate_drops_duplicates_and_tags_the_rest(config, candidate, image):
    previous = _edition(candidate_id=candidate.id)
    other = Candidate(source="s", source_url="u", title="Something else entirely",
                      image=image.model_copy(update={"url": "https://x/other.jpg"}),
                      categories=["nature"], keywords=["glacier"], entities=["Iceland"])
    survivors = Deduplicator.from_history(config, [previous]).annotate([candidate, other])
    assert [c.title for c in survivors] == ["Something else entirely"]
    assert "duplicate_similarity" in other.raw


def test_similarity_helpers():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert overlap_coefficient({"a"}, {"a", "b", "c"}) == 1.0
    assert "photograph" not in tokens("A photograph of a glacier")


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #
def test_a_fresh_domain_scores_a_larger_bonus(config, image):
    history = [_edition(categories=["space"], edition_date=date.today() - timedelta(days=1))]
    state = RotationState.from_history(config, history)
    just_ran = Candidate(source="s", source_url="u", title="More space",
                         image=image, categories=["space"])
    fresh = Candidate(source="s", source_url="u", title="Something musical",
                      image=image, categories=["music"])
    assert rotation_bonus(fresh, state, config) > rotation_bonus(just_ran, state, config)


def test_music_is_boosted_when_under_target(config, image):
    history = [_edition(categories=["space"]) for _ in range(5)]
    state = RotationState.from_history(config, history)
    assert state.music_share == 0.0
    music = Candidate(source="s", source_url="u", title="A console", image=image,
                      categories=["music"], music=MusicMeta(genre=["rock"]))
    other = Candidate(source="s", source_url="u", title="A glacier", image=image,
                      categories=["nature"])
    assert rotation_bonus(music, state, config) >= rotation_bonus(other, state, config)


def test_global_balance_favours_under_represented_regions(config, image):
    history = [
        _edition(issue_number=i, categories=["music"],
                 music=MusicMeta(country=["united_states"]),
                 edition_date=date.today() - timedelta(days=i * 2))
        for i in range(1, 5)
    ]
    state = RotationState.from_history(config, history)
    assert state.dominant_music_share > config.music.global_balance.max_dominant_share
    indian = Candidate(source="s", source_url="u", title="A sarangi", image=image,
                       categories=["music"], music=MusicMeta(country=["india"]))
    american = Candidate(source="s", source_url="u", title="A Fender", image=image,
                         categories=["music"], music=MusicMeta(country=["united_states"]))
    assert rotation_bonus(indian, state, config) > rotation_bonus(american, state, config)


def test_cross_domain_bonus_is_capped(config, image):
    many = Candidate(source="s", source_url="u", title="t", image=image,
                     categories=["music", "engineering", "history", "space", "culture"])
    assert cross_domain_bonus(many, config) == config.scoring.cross_domain_bonus.max
    single = Candidate(source="s", source_url="u", title="t", image=image,
                       categories=["history"])
    assert cross_domain_bonus(single, config) == 0.0


# --------------------------------------------------------------------------- #
# Prefilter scoring
# --------------------------------------------------------------------------- #
def test_prefilter_prefers_the_question_raising_candidate(config, candidate, image):
    generic = Candidate(
        source="met_museum", source_url="u", title="Flag of a country",
        description="Official logo.",
        image=ImageAsset(url="https://x/y.png", width=700, height=500,
                         license=LicenseInfo(id="cc0", reusable=True,
                                             requires_attribution=False)),
    )
    good, _ = prefilter_score(candidate, config)
    poor, _ = prefilter_score(generic, config)
    assert good > poor + 30


def test_prefilter_cuts_to_the_configured_budget(config, candidate, image):
    many = []
    for index in range(60):
        many.append(candidate.model_copy(update={
            "id": f"c{index}",
            "image": image.model_copy(update={"url": f"https://x/{index}.jpg"}),
        }))
    kept = apply_prefilter(many, config)
    assert len(kept) == config.pipeline.prefilter_keep


def test_prefilter_rewards_public_domain_over_share_alike(config, candidate):
    pd_score, _ = prefilter_score(candidate, config)
    share_alike = candidate.model_copy(update={
        "image": candidate.image.model_copy(update={
            "license": LicenseInfo(id="cc-by-sa-4.0", reusable=True, share_alike=True,
                                   requires_attribution=True)
        })
    })
    sa_score, _ = prefilter_score(share_alike, config)
    assert pd_score > sa_score


def test_source_authority_moves_the_prefilter_score(config, candidate):
    high, _ = prefilter_score(candidate, config, {"wikimedia_potd": 95})
    low, _ = prefilter_score(candidate, config, {"wikimedia_potd": 40})
    assert high > low


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
def test_taxonomy_detects_music_metadata(config):
    text = "A progressive rock band recording at Abbey Road Studios in London, 1973"
    music = detect_music(text, config=config.music)
    assert music is not None
    assert "progressive_rock" in music.subgenre
    assert "rock" in music.genre
    assert "united_kingdom" in music.country
    assert "1970s" in music.era


def test_taxonomy_returns_none_for_non_music(config):
    assert detect_music("A glacier calving into a fjord", config=config.music) is None


def test_taxonomy_finds_cross_domain_stories(config):
    domains = classify_domains(
        "The concert hall's acoustics were designed using a scale model and "
        "measurements of reverberation time",
        allowed=config.content.categories,
    )
    assert "architecture" in domains
    assert "music" in domains


def test_era_extraction():
    assert "1960s" in era_from_text("recorded in 1967")
    assert "pre_1900" in era_from_text("built in 1873")


def test_score_card_dimensions_are_numeric():
    card = ScoreCard(visual_score=80, story_score=70)
    dimensions = card.dimensions()
    assert dimensions["visual_score"] == 80
    assert "overall" not in dimensions


# --------------------------------------------------------------------------- #
# Recency decay
# --------------------------------------------------------------------------- #
def test_recency_weight_decays_to_nothing(config):
    dedup = Deduplicator.from_history(config, [])
    recent = _edition(edition_date=date.today() - timedelta(days=3))
    middling = _edition(edition_date=date.today()
                        - timedelta(days=config.content.avoid_recent_days + 30))
    ancient = _edition(edition_date=date.today() - timedelta(days=1000))
    assert dedup.recency_weight(recent) == 1.0
    assert 0.0 < dedup.recency_weight(middling) < 1.0
    assert dedup.recency_weight(ancient) == 0.0
