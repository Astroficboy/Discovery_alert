"""The copyright gate.

The rule this module enforces: **an image is unusable until a recognised,
reuse-permitting licence has been positively identified.** There is no
"probably fine" branch. Anything we cannot classify is dropped, which is the
correct failure mode for a newsletter that emails photographs to people.

Everything here is pure string work over metadata the archives already give
us, so it runs on all 120 discovery candidates for free, before a single
token is spent.
"""

from __future__ import annotations

import re

from .models import ImageAsset, LicenseInfo

# --------------------------------------------------------------------------- #
# The allowlist. Keys are matched against normalised licence strings.
# --------------------------------------------------------------------------- #
_CC_URL = "https://creativecommons.org/licenses/{code}/{ver}/"

_KNOWN: dict[str, LicenseInfo] = {
    "pd": LicenseInfo(
        id="pd", name="Public domain", reusable=True,
        requires_attribution=False, commercial_ok=True,
    ),
    "pd-us-gov": LicenseInfo(
        id="pd-us-gov", name="Public domain (work of the U.S. federal government)",
        reusable=True, requires_attribution=False, commercial_ok=True,
    ),
    "pd-nasa": LicenseInfo(
        id="pd-nasa", name="Public domain (NASA)",
        url="https://www.nasa.gov/nasa-brand-center/images-and-media/",
        reusable=True, requires_attribution=False, commercial_ok=True,
    ),
    "pd-old": LicenseInfo(
        id="pd-old", name="Public domain (copyright expired)", reusable=True,
        requires_attribution=False, commercial_ok=True,
    ),
    "cc0": LicenseInfo(
        id="cc0", name="CC0 1.0 (public domain dedication)",
        url="https://creativecommons.org/publicdomain/zero/1.0/",
        reusable=True, requires_attribution=False, commercial_ok=True,
    ),
    "cc-by-2.0": LicenseInfo(
        id="cc-by-2.0", name="CC BY 2.0", url=_CC_URL.format(code="by", ver="2.0"),
        reusable=True, requires_attribution=True, commercial_ok=True,
    ),
    "cc-by-2.5": LicenseInfo(
        id="cc-by-2.5", name="CC BY 2.5", url=_CC_URL.format(code="by", ver="2.5"),
        reusable=True, requires_attribution=True, commercial_ok=True,
    ),
    "cc-by-3.0": LicenseInfo(
        id="cc-by-3.0", name="CC BY 3.0", url=_CC_URL.format(code="by", ver="3.0"),
        reusable=True, requires_attribution=True, commercial_ok=True,
    ),
    "cc-by-4.0": LicenseInfo(
        id="cc-by-4.0", name="CC BY 4.0", url=_CC_URL.format(code="by", ver="4.0"),
        reusable=True, requires_attribution=True, commercial_ok=True,
    ),
    "cc-by-sa-2.0": LicenseInfo(
        id="cc-by-sa-2.0", name="CC BY-SA 2.0", url=_CC_URL.format(code="by-sa", ver="2.0"),
        reusable=True, requires_attribution=True, share_alike=True, commercial_ok=True,
    ),
    "cc-by-sa-2.5": LicenseInfo(
        id="cc-by-sa-2.5", name="CC BY-SA 2.5", url=_CC_URL.format(code="by-sa", ver="2.5"),
        reusable=True, requires_attribution=True, share_alike=True, commercial_ok=True,
    ),
    "cc-by-sa-3.0": LicenseInfo(
        id="cc-by-sa-3.0", name="CC BY-SA 3.0", url=_CC_URL.format(code="by-sa", ver="3.0"),
        reusable=True, requires_attribution=True, share_alike=True, commercial_ok=True,
    ),
    "cc-by-sa-4.0": LicenseInfo(
        id="cc-by-sa-4.0", name="CC BY-SA 4.0", url=_CC_URL.format(code="by-sa", ver="4.0"),
        reusable=True, requires_attribution=True, share_alike=True, commercial_ok=True,
    ),
    "ogl-3.0": LicenseInfo(
        id="ogl-3.0", name="Open Government Licence v3.0",
        url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
        reusable=True, requires_attribution=True, commercial_ok=True,
    ),
    "gfdl": LicenseInfo(
        id="gfdl", name="GNU Free Documentation License",
        url="https://www.gnu.org/licenses/fdl-1.3.html",
        reusable=True, requires_attribution=True, share_alike=True, commercial_ok=True,
    ),
}

#: Licences we can read but must refuse: no commercial clause, no derivatives,
#: or "free for non-commercial editorial use" style grants. A personal
#: newsletter is arguably non-commercial, but "arguably" is not a licence.
_REFUSED = {
    "cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd", "cc-by-nd",
    "rights-reserved", "in-copyright", "all-rights-reserved", "fair-use",
    "editorial-use-only", "non-commercial", "unknown", "no-known-copyright-holder",
}

_CC_PATTERN = re.compile(
    r"\bcc[\s_-]*(by(?:[\s_-]*(?:nc|nd|sa))*|zero|0)\b[\s_-]*(\d(?:\.\d)?)?", re.IGNORECASE
)

_PD_PHRASES = (
    "public domain",
    "publicdomain",
    "pd-us",
    "pd-old",
    "no known copyright",
    # The Library of Congress's standard formulation for material it has
    # determined carries no publication restrictions.
    "no known restrictions on publication",
    "no known restrictions",
    "copyright expired",
    "out of copyright",
    "gemeinfrei",
)

_GOV_PHRASES = (
    "work of the united states government",
    "u.s. government work",
    "us government work",
    "usgovernment",
    "pd-usgov",
    "nasa image use policy",
)


def normalise_license_string(raw: str | None) -> str:
    if not raw:
        return ""
    text = raw.strip().lower()
    text = re.sub(r"https?://creativecommons\.org/(licenses|publicdomain)/", "cc-", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-/ ")


def classify(raw: str | None, *, hints: str = "") -> LicenseInfo:
    """Map a free-form licence string onto the allowlist.

    ``hints`` may carry extra context (institution name, rights statement,
    template names) that only ever helps identify a *permissive* licence - it
    can never upgrade an explicitly restrictive one.
    """
    raw_text = (raw or "").strip()
    haystack = f"{raw_text} {hints}".lower()
    norm = normalise_license_string(raw_text)

    # Explicit refusals win over everything, including generous-sounding hints.
    for bad in ("nc", "nd"):
        patterns = (
            rf"\bcc[-\s]?by[-\s]?(?:sa[-\s]?)?{bad}\b",      # "CC BY-NC", "cc by nc sa"
            rf"licenses?/by-(?:sa-)?{bad}\b",                  # creativecommons.org URLs
        )
        if any(re.search(pattern, haystack) for pattern in patterns):
            return LicenseInfo(id=f"cc-by-{bad}", name=f"CC BY-{bad.upper()} (not reusable here)",
                               reusable=False, raw=raw_text)
    if "noncommercial" in haystack or "non-commercial" in haystack:
        return LicenseInfo(id="non-commercial", name="Non-commercial licence (not reusable here)",
                           reusable=False, raw=raw_text)
    if "noderiv" in haystack or "no-deriv" in haystack:
        return LicenseInfo(id="cc-by-nd", name="No-derivatives licence (not reusable here)",
                           reusable=False, raw=raw_text)
    if any(term in haystack for term in ("all rights reserved", "editorial use only",
                                         "rights reserved", "in copyright")):
        return LicenseInfo(id="rights-reserved", name="Rights reserved", reusable=False,
                           raw=raw_text)

    if norm in _KNOWN:
        return _KNOWN[norm].model_copy(update={"raw": raw_text})

    if "cc0" in haystack or "publicdomain/zero" in haystack or norm.startswith("cc-zero"):
        return _KNOWN["cc0"].model_copy(update={"raw": raw_text})

    match = _CC_PATTERN.search(haystack)
    if match:
        code = re.sub(r"[\s_]+", "-", match.group(1).lower())
        version = match.group(2) or "4.0"
        if len(version) == 1:
            version = f"{version}.0"
        key = f"cc-{code}-{version}"
        if key in _KNOWN:
            return _KNOWN[key].model_copy(update={"raw": raw_text})

    if any(phrase in haystack for phrase in _GOV_PHRASES):
        return _KNOWN["pd-us-gov"].model_copy(update={"raw": raw_text})
    if any(phrase in haystack for phrase in _PD_PHRASES):
        return _KNOWN["pd"].model_copy(update={"raw": raw_text})
    if "open government licence" in haystack or norm.startswith("ogl"):
        return _KNOWN["ogl-3.0"].model_copy(update={"raw": raw_text})
    if norm.startswith("gfdl") or "free documentation license" in haystack:
        return _KNOWN["gfdl"].model_copy(update={"raw": raw_text})

    return LicenseInfo(id="unknown", name=raw_text or "Unknown", reusable=False, raw=raw_text)


def is_usable(license_info: LicenseInfo) -> bool:
    return bool(license_info.reusable) and license_info.id not in _REFUSED


def credit_line(image: ImageAsset) -> str:
    """Build the attribution string that goes under the hero image.

    Public-domain works still get a provenance line - it is good editorial
    practice and it tells the reader where to go and look for themselves.
    """
    parts: list[str] = []
    if image.title:
        parts.append(f"“{image.title.strip()}”")
    if image.creator:
        parts.append(image.creator.strip())
    if image.created:
        parts.append(str(image.created).strip())
    if image.institution:
        parts.append(image.institution.strip())

    lic = image.license
    if lic.is_public_domain:
        parts.append(lic.name)
    elif lic.reusable:
        parts.append(f"licensed under {lic.name}")
    else:  # pragma: no cover - unusable images never reach the renderer
        parts.append("licence unclear - not for reuse")

    return " · ".join(p for p in parts if p)


def vet_image(image: ImageAsset, *, hints: str = "") -> tuple[bool, str]:
    """Classify, gate and annotate an image in one call.

    Returns ``(usable, reason)`` and, when usable, fills in
    ``image.license`` and ``image.credit`` in place.
    """
    lic = classify(image.license.raw or image.license.id or image.license.name, hints=hints)
    image.license = lic
    if not is_usable(lic):
        return False, f"licence not clearly reusable ({lic.name or 'unknown'})"
    if lic.requires_attribution and not (image.creator or image.institution):
        return False, "attribution required but no creator or institution recorded"
    image.credit = credit_line(image)
    return True, "ok"


__all__ = ["classify", "credit_line", "is_usable", "normalise_license_string", "vet_image"]
