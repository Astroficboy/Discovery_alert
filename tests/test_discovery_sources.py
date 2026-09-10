"""Source parsers, against recorded API responses.

No test here touches the network: every response comes from
``tests/fixtures/`` through an ``httpx.MockTransport``.
"""

from __future__ import annotations

import pytest

from src.config import SourceConfig
from src.discovery.europeana import Europeana
from src.discovery.fixtures import BundledFixtures
from src.discovery.loc import LibraryOfCongress
from src.discovery.met import MetMuseum
from src.discovery.nasa import NasaApod, _apod_slug
from src.discovery.smithsonian import Smithsonian
from src.discovery.wikimedia import WikimediaFeatured
from src.discovery.wikipedia import WikipediaFeaturedFeed
from tests.conftest import load_fixture, mock_http


async def _run(source_cls, config, routes, *, options=None, limit=10, api_key=None):
    if api_key and source_cls.requires_key:
        config.source_api_keys[source_cls.requires_key] = api_key
    async with mock_http(routes) as http:
        source = source_cls(
            config, SourceConfig(name=source_cls.name, limit=limit, options=options or {}), http
        )
        return await source.discover()


# --------------------------------------------------------------------------- #
# Wikimedia Commons
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_commons_parses_and_gates(config):
    routes = {"commons.wikimedia.org": load_fixture("commons_categorymembers.json")}
    candidates = await _run(WikimediaFeatured, config, routes,
                            options={"categories": ["Featured pictures of history"]})
    # Three records in, one out: the copyrighted one and the tiny one are gated.
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Blast furnace at Völklingen"
    assert candidate.image.license.id == "cc-by-sa-4.0"
    assert candidate.image.license.requires_attribution
    assert candidate.image.creator == "A. Photographer"
    assert candidate.image.width == 1200  # the width-capped rendering
    assert "<p>" not in candidate.description  # HTML stripped from extmetadata
    assert "abandoned" in candidate.description.lower()
    assert candidate.image.credit and "CC BY-SA 4.0" in candidate.image.credit


@pytest.mark.asyncio
async def test_commons_infers_domains(config):
    routes = {"commons.wikimedia.org": load_fixture("commons_categorymembers.json")}
    candidates = await _run(WikimediaFeatured, config, routes)
    assert "engineering" in candidates[0].categories


@pytest.mark.asyncio
async def test_source_failure_is_isolated(config):
    """A dead archive returns nothing and raises nothing."""
    async with mock_http({}, default_status=503) as http:
        source = WikimediaFeatured(config, SourceConfig(name="wikimedia_featured", limit=5), http)
        assert await source.discover() == []


# --------------------------------------------------------------------------- #
# NASA
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_apod_skips_photographer_copyright_and_video(config):
    routes = {"api.nasa.gov": load_fixture("nasa_apod.json")}
    candidates = await _run(NasaApod, config, routes)
    titles = [c.title for c in candidates]
    assert titles == ["The Horsehead Nebula in Infrared"]
    assert candidates[0].image.license.id == "pd-us-gov"
    assert "space" in candidates[0].categories


def test_apod_permalink_slug():
    assert _apod_slug("2026-09-08") == "260908"
    assert _apod_slug("nonsense") == ""


# --------------------------------------------------------------------------- #
# Met
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_met_takes_only_public_domain(config):
    routes = {
        "/search": {"objectIDs": [501234, 501235]},
        "/objects/501234": load_fixture("met_object.json"),
        "/objects/501235": load_fixture("met_object_copyright.json"),
    }
    candidates = await _run(MetMuseum, config, routes,
                            options={"departments": ["Musical Instruments"]})
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Sarangi"
    assert candidate.image.license.id == "cc0"
    assert candidate.is_music
    assert "instruments" in (candidate.music.subjects if candidate.music else [])


@pytest.mark.asyncio
async def test_met_unknown_department_is_skipped(config):
    candidates = await _run(MetMuseum, config, {}, options={"departments": ["Cheese"]})
    assert candidates == []


# --------------------------------------------------------------------------- #
# Library of Congress
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_loc_uses_item_rights_and_drops_unevaluated(config):
    routes = {"loc.gov": load_fixture("loc_results.json")}
    candidates = await _run(LibraryOfCongress, config, routes,
                            options={"collections": ["fsa-owi-photos"]})
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.image.license.reusable
    assert candidate.image.creator == "Palmer, Alfred T."
    assert candidate.image.url.endswith("1a34765r.jpg")  # largest rendering
    assert "photography" in candidate.categories


@pytest.mark.asyncio
async def test_loc_collection_rights_can_be_asserted(config):
    """A collection-level rights statement fills in where item rights are absent."""
    payload = {"results": [{
        "id": "https://www.loc.gov/item/1/",
        "title": "Untitled",
        "image_url": ["https://tile.loc.gov/a.jpg"],
        "item": {},
    }]}
    candidates = await _run(LibraryOfCongress, config, {"loc.gov": payload},
                            options={"collections": ["civil-war-glass-negatives"]})
    assert len(candidates) == 1
    assert candidates[0].image.license.reusable


# --------------------------------------------------------------------------- #
# Smithsonian / Europeana
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_smithsonian_requires_a_key(config):
    config.source_api_keys.pop("smithsonian", None)
    candidates = await _run(Smithsonian, config, {})
    assert candidates == []


@pytest.mark.asyncio
async def test_smithsonian_takes_only_cc0_media(config):
    routes = {"api.si.edu": load_fixture("smithsonian_search.json")}
    candidates = await _run(Smithsonian, config, routes, api_key="test-key",
                            options={"queries": ["music"]})
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Moog Modular Synthesizer"
    assert candidate.is_music
    assert "Robert Moog" in candidate.entities


@pytest.mark.asyncio
async def test_europeana_rejects_non_commercial(config):
    routes = {"api.europeana.eu": load_fixture("europeana_search.json")}
    candidates = await _run(Europeana, config, routes, api_key="test-key",
                            options={"queries": ["instrument"]})
    assert len(candidates) == 1
    assert candidates[0].title == "Hurdy-gurdy from Hungary"
    assert candidates[0].location_hint == "Hungary"


# --------------------------------------------------------------------------- #
# Wikipedia
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wikipedia_potd_parses_licence(config):
    routes = {"api.wikimedia.org": load_fixture("wikipedia_feed.json")}
    candidates = await _run(WikipediaFeaturedFeed, config, routes, limit=2)
    # The POTD carries a licence; the featured article's lead image does not,
    # so the gate correctly drops it.
    assert all(c.image.license.reusable for c in candidates)
    assert any("Sarangi" in c.title for c in candidates)
    assert not any("Krakatoa" in c.title for c in candidates)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bundled_fixtures_are_silent_unless_starved(config):
    async with mock_http({}) as http:
        source = BundledFixtures(config, SourceConfig(name="fixtures", limit=10), http)
        assert await source.discover() == []
        source.starved = True
        assert len(await source.discover()) >= 4


def test_bundled_fixtures_all_pass_the_licence_gate(config):
    import asyncio

    async def check():
        async with mock_http({}) as http:
            source = BundledFixtures(config, SourceConfig(name="fixtures", limit=20), http)
            loaded = source.load()
            gated = source.apply_gates(loaded)
            assert len(gated) >= 4, "bundled fixtures should survive their own licence gate"
            for candidate in gated:
                assert candidate.image.license.reusable
                assert candidate.image.credit

    asyncio.run(check())
