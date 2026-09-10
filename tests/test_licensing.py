"""The copyright gate. The most consequential module in the project."""

from __future__ import annotations

import pytest

from src.licensing import classify, credit_line, is_usable, vet_image
from src.models import ImageAsset, LicenseInfo


@pytest.mark.parametrize("raw,expected_id", [
    ("cc-by-sa-4.0", "cc-by-sa-4.0"),
    ("CC BY 2.5", "cc-by-2.5"),
    ("CC BY-SA 3.0", "cc-by-sa-3.0"),
    ("cc by 4.0", "cc-by-4.0"),
    ("cc0", "cc0"),
    ("CC0 1.0 (Met Open Access)", "cc0"),
    ("Public domain", "pd"),
    ("No known restrictions on publication", "pd"),
    ("no known copyright restrictions", "pd"),
    ("NASA image use policy: public domain", "pd-us-gov"),
    ("Open Government Licence v3.0", "ogl-3.0"),
    ("GFDL", "gfdl"),
])
def test_permissive_licences_are_recognised(raw, expected_id):
    licence = classify(raw)
    assert licence.id == expected_id
    assert licence.reusable is True
    assert is_usable(licence)


@pytest.mark.parametrize("raw", [
    "All rights reserved",
    "Editorial use only",
    "CC BY-NC 4.0",
    "cc-by-nc-sa-4.0",
    "http://creativecommons.org/licenses/by-nc-nd/4.0/",
    "Attribution-NonCommercial",
    "Attribution-NoDerivs 3.0",
    "Rights status not evaluated",
    "In copyright",
    "",
    None,
    "some licence nobody has ever heard of",
])
def test_anything_not_clearly_permissive_is_refused(raw):
    licence = classify(raw)
    assert licence.reusable is False
    assert not is_usable(licence)


def test_restrictive_terms_beat_generous_hints():
    """A hint must never be able to upgrade an explicit restriction."""
    licence = classify("All rights reserved", hints="NASA public domain Smithsonian CC0")
    assert licence.reusable is False


def test_hints_can_identify_a_permissive_licence():
    licence = classify("", hints="This is a work of the United States Government")
    assert licence.id == "pd-us-gov"
    assert licence.reusable is True


def test_vet_image_fills_in_credit_for_usable_image():
    image = ImageAsset(url="https://x/y.jpg", title="A thing", creator="A. Maker",
                       created="1912", institution="An Archive",
                       license=LicenseInfo(raw="CC BY-SA 4.0"))
    usable, reason = vet_image(image)
    assert usable and reason == "ok"
    assert image.credit
    assert "A. Maker" in image.credit
    assert "CC BY-SA 4.0" in image.credit


def test_vet_image_refuses_attribution_licence_without_a_creator():
    image = ImageAsset(url="https://x/y.jpg", license=LicenseInfo(raw="CC BY 4.0"))
    usable, reason = vet_image(image)
    assert not usable
    assert "attribution required" in reason


def test_public_domain_needs_no_creator():
    image = ImageAsset(url="https://x/y.jpg", title="Old thing",
                       license=LicenseInfo(raw="Public domain"))
    usable, _ = vet_image(image)
    assert usable


def test_credit_line_names_the_licence_for_share_alike():
    image = ImageAsset(url="https://x/y.jpg", title="T", creator="C",
                       institution="I", license=classify("CC BY-SA 4.0"))
    line = credit_line(image)
    assert "licensed under CC BY-SA 4.0" in line
    assert line.count("·") >= 2


def test_public_domain_credit_still_records_provenance():
    image = ImageAsset(url="https://x/y.jpg", title="T", creator="C",
                       institution="NASA", license=classify("Public domain"))
    assert "NASA" in credit_line(image)
    assert "Public domain" in credit_line(image)
