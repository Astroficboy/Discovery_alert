"""Classifying a candidate into editorial domains, cheaply.

This runs on every discovered candidate before any model is called, so it is
pure keyword work over titles, captions and archive categories. It is
deliberately generous: a candidate that *might* be about music gets the music
tag, and the triage model later decides whether the story is real.

Two things come out of it:

* ``domains`` - which of the top-level categories the candidate touches. More
  than one is a good sign; cross-domain stories are the ones worth sending.
* ``MusicMeta`` - genre, era, region and subject vocabulary for music
  candidates, stored as metadata rather than as a rigid bucket.
"""

from __future__ import annotations

import re

from ..config import MusicConfig
from ..models import MusicMeta

# --------------------------------------------------------------------------- #
# Domain vocabulary
# --------------------------------------------------------------------------- #
DOMAIN_TERMS: dict[str, tuple[str, ...]] = {
    "history": (
        "history", "historic", "archive", "archival", "century", "medieval",
        "ancient", "wwi", "wwii", "world war", "cold war", "empire", "dynasty",
        "revolution", "colonial", "antiquity", "excavation", "archaeolog",
        "manuscript", "artifact", "artefact", "ruins", "relic", "treaty",
        "1800s", "1900s", "victorian", "renaissance", "prehistoric",
    ),
    "science": (
        "science", "scientific", "physics", "chemistry", "biology", "microscop",
        "specimen", "laborator", "experiment", "discovery", "genome", "cell",
        "neuroscience", "quantum", "particle", "isotope", "crystall", "fossil",
        "evolution", "bacteri", "virus", "molecul", "spectro", "radiation",
    ),
    "space": (
        "space", "nasa", "esa", "apollo", "gemini", "voyager", "cassini",
        "astronaut", "cosmonaut", "satellite", "orbit", "spacecraft", "rocket",
        "launch", "nebula", "galaxy", "galaxies", "planet", "lunar", "mars",
        "jupiter", "saturn", "telescope", "hubble", "webb", "eclipse", "comet",
        "asteroid", "solar", "supernova", "black hole", "observatory",
    ),
    "nature": (
        "nature", "wildlife", "animal", "species", "bird", "insect", "mammal",
        "forest", "jungle", "desert", "ocean", "reef", "glacier", "volcano",
        "geolog", "canyon", "cave", "mountain", "river", "storm", "aurora",
        "ecosystem", "migration", "deep sea", "abyssal", "tundra", "wetland",
        "eruption", "predator", "bloom",
    ),
    "exploration": (
        "expedition", "explorer", "exploration", "voyage", "summit", "everest",
        "antarctic", "arctic", "polar", "shackleton", "amundsen", "circumnavig",
        "descent", "bathyscaphe", "submersible", "trek", "first ascent",
        "uncharted", "remote island", "caving", "speleolog",
    ),
    "engineering": (
        "engineering", "engineer", "bridge", "dam", "tunnel", "railway",
        "locomotive", "turbine", "reactor", "factory", "foundry", "shipyard",
        "crane", "machine", "mechanism", "aircraft", "airship", "submarine",
        "steam", "hydraulic", "girder", "construction", "infrastructure",
        "pipeline", "excavator", "assembly line", "prototype",
    ),
    "technology": (
        "technology", "computer", "computing", "transistor", "microchip",
        "semiconductor", "telegraph", "telephone", "radio", "television",
        "punch card", "mainframe", "circuit", "robot", "automaton", "camera",
        "lens", "film stock", "magnetic tape", "vacuum tube", "valve",
        "network", "arpanet", "algorithm", "cryptograph", "enigma",
    ),
    "architecture": (
        "architect", "architecture", "building", "cathedral", "temple",
        "mosque", "palace", "fortress", "castle", "skyscraper", "facade",
        "dome", "vault", "brutalis", "abandoned", "ruined building",
        "underground city", "megaproject", "monument", "pavilion", "stadium",
        "concert hall", "opera house",
    ),
    "people": (
        "portrait", "biograph", "inventor", "pioneer", "self-portrait",
        "photographer", "scientist who", "nurse", "worker", "survivor",
        "veteran", "child of", "family of", "crew of", "first woman",
        "first man", "unknown soldier",
    ),
    "culture": (
        "culture", "tradition", "ritual", "festival", "ceremony", "folklore",
        "costume", "textile", "cuisine", "language", "calligraph", "religion",
        "pilgrimage", "carnival", "mask", "dance", "theatre", "puppet",
        "art history", "painting", "sculpture", "printmaking", "poster",
    ),
    "photography": (
        "photograph", "daguerreotype", "ambrotype", "tintype", "glass negative",
        "collodion", "autochrome", "kodachrome", "photojournal", "darkroom",
        "exposure", "shutter", "lens flare", "photo essay", "contact sheet",
        "stereograph", "panoramic photograph",
    ),
    "mystery": (
        "mystery", "unexplained", "disappear", "vanished", "unsolved",
        "legend", "myth", "hoax", "enigma", "rumour", "lost city",
        "undeciphered", "cipher",
    ),
    "music": (),  # filled from MUSIC_TERMS below
}

# --------------------------------------------------------------------------- #
# Music vocabulary
# --------------------------------------------------------------------------- #
MUSIC_TERMS: tuple[str, ...] = (
    "music", "musician", "musical", "band", "orchestra", "ensemble", "choir",
    "concert", "gig", "festival stage", "recital", "symphony", "sonata",
    "album", "record label", "recording", "studio", "sound engineer",
    "producer", "mixing desk", "mixing console", "multitrack", "tape machine",
    "vinyl", "gramophone", "phonograph", "turntable", "cassette", "bootleg",
    "instrument", "guitar", "bass guitar", "drum", "drums", "drum kit",
    "percussion", "cymbal", "tabla", "mridangam", "taiko", "djembe", "conga",
    "sitar", "sarod", "veena", "shehnai", "santoor", "piano", "harpsichord",
    "organ", "violin", "cello", "harp", "flute", "clarinet", "saxophone",
    "trumpet", "trombone", "accordion", "bagpipe", "banjo", "mandolin",
    "koto", "shamisen", "erhu", "gamelan", "kora", "oud", "balalaika",
    "synthesizer", "synthesiser", "moog", "theremin", "mellotron", "sampler",
    "drum machine", "sequencer", "midi", "amplifier", "loudspeaker",
    "microphone", "vocalist", "singer", "songwriter", "composer", "conductor",
    "jazz", "blues", "rock", "metal", "punk", "reggae", "hip hop", "hip-hop",
    "techno", "house music", "ambient", "folk song", "raga", "carnatic",
    "hindustani", "opera singer", "busker", "sound recording", "radiophonic",
)
DOMAIN_TERMS["music"] = MUSIC_TERMS

GENRE_TERMS: dict[str, tuple[str, ...]] = {
    "progressive_rock": ("progressive rock", "prog rock", "art rock"),
    "psychedelic_rock": ("psychedelic", "acid rock"),
    "classic_rock": ("rock and roll", "rock 'n' roll", "rock band", "classic rock"),
    "heavy_metal": ("heavy metal", "metal band", "nwobhm"),
    "thrash_metal": ("thrash metal",),
    "black_metal": ("black metal",),
    "death_metal": ("death metal",),
    "doom_metal": ("doom metal", "sludge"),
    "punk": ("punk rock", "punk band", "punk scene"),
    "hardcore_punk": ("hardcore punk", "straight edge"),
    "post_punk": ("post-punk", "post punk"),
    "bebop": ("bebop", "be-bop"),
    "free_jazz": ("free jazz",),
    "fusion": ("jazz fusion", "jazz-rock"),
    "swing": ("big band", "swing era"),
    "delta_blues": ("delta blues",),
    "chicago_blues": ("chicago blues",),
    "electric_blues": ("electric blues",),
    "baroque": ("baroque music", "harpsichord", "basso continuo"),
    "romantic": ("romantic era", "romanticism music"),
    "minimalism": ("minimalist music", "minimalism"),
    "opera": ("opera", "operatic"),
    "musique_concrete": ("musique concrète", "musique concrete", "tape music"),
    "ambient": ("ambient music", "drone music"),
    "techno": ("techno",),
    "house": ("house music", "chicago house", "acid house"),
    "drum_and_bass": ("drum and bass", "drum & bass", "jungle music"),
    "dub": ("dub reggae", "dub plate", "dubwise"),
    "reggae": ("reggae", "rocksteady"),
    "ska": ("ska",),
    "hindustani": ("hindustani", "khyal", "dhrupad", "sitar", "sarod", "tabla"),
    "carnatic": ("carnatic", "mridangam", "veena", "kriti"),
    "gamelan": ("gamelan",),
    "throat_singing": ("throat singing", "khoomei", "overtone singing"),
    "african_traditional": ("griot", "kora", "mbira", "highlife", "afrobeat"),
    "old_school_hip_hop": ("old school hip hop", "block party", "breakbeat"),
    "synth_pop": ("synth-pop", "synthpop"),
    "city_pop": ("city pop",),
    "folk": ("folk music", "folk song", "field recording"),
    "appalachian": ("appalachian", "old-time music"),
    "celtic": ("celtic music", "uilleann", "bodhran"),
}

MUSIC_SUBJECT_TERMS: dict[str, tuple[str, ...]] = {
    "instruments": ("instrument", "guitar", "sitar", "piano", "violin", "flute",
                    "organ", "harp", "accordion", "banjo", "oud", "koto"),
    "percussion": ("drum", "percussion", "cymbal", "tabla", "mridangam", "taiko",
                   "djembe", "conga", "timpani", "frame drum"),
    "recording": ("recording", "microphone", "tape machine", "multitrack",
                  "sound recording", "phonograph", "wax cylinder"),
    "production": ("producer", "mixing", "mastering", "overdub", "studio session"),
    "studios": ("recording studio", "abbey road", "sound studio", "control room"),
    "technology": ("synthesizer", "synthesiser", "moog", "theremin", "sampler",
                   "drum machine", "sequencer", "midi", "amplifier", "effects pedal"),
    "concerts": ("concert", "live performance", "gig", "tour", "recital"),
    "festivals": ("festival", "woodstock", "monterey", "glastonbury"),
    "venues": ("venue", "concert hall", "opera house", "club", "auditorium"),
    "musicians": ("musician", "singer", "guitarist", "drummer", "pianist",
                  "composer", "conductor", "vocalist", "band"),
    "albums": ("album", "lp record", "record sleeve", "album cover"),
    "songs": ("song", "single", "ballad", "anthem"),
    "radio": ("radio broadcast", "radio station", "wireless broadcast"),
    "labels": ("record label", "records inc", "recording company"),
    "music_history": ("music history", "musicology", "genre", "scene"),
    "music_culture": ("counterculture", "subculture", "youth culture", "protest song"),
    "music_science": ("acoustics", "psychoacoustics", "resonance", "harmonic series"),
}

REGION_TERMS: dict[str, tuple[str, ...]] = {
    "india": ("india", "indian", "delhi", "mumbai", "bengal", "tamil", "kerala",
              "punjab", "rajasthan", "chennai", "kolkata"),
    "japan": ("japan", "japanese", "tokyo", "kyoto", "osaka"),
    "china": ("china", "chinese", "beijing", "shanghai"),
    "korea": ("korea", "korean", "seoul"),
    "united_states": ("united states", "american", "u.s.", "usa", "new york",
                      "chicago", "detroit", "memphis", "new orleans", "california"),
    "united_kingdom": ("united kingdom", "british", "england", "london",
                       "scotland", "wales", "manchester", "liverpool"),
    "germany": ("germany", "german", "berlin", "cologne", "düsseldorf"),
    "france": ("france", "french", "paris"),
    "brazil": ("brazil", "brazilian", "rio de janeiro", "bahia"),
    "jamaica": ("jamaica", "jamaican", "kingston"),
    "nigeria": ("nigeria", "nigerian", "lagos"),
    "mali": ("mali", "malian", "bamako"),
    "russia": ("russia", "russian", "soviet", "moscow", "leningrad"),
    "indonesia": ("indonesia", "javanese", "balinese", "jakarta"),
    "australia": ("australia", "australian", "sydney", "melbourne"),
    "mexico": ("mexico", "mexican"),
    "egypt": ("egypt", "egyptian", "cairo"),
    "iran": ("iran", "persian", "tehran"),
    "turkey": ("turkey", "turkish", "istanbul", "ottoman"),
    "scandinavia": ("norway", "sweden", "denmark", "finland", "iceland", "nordic"),
}

_YEAR = re.compile(r"\b(1[5-9]\d{2}|20[0-2]\d)\b")
_DECADE = re.compile(r"\b(1[89]\d0|20[012]0)s\b")


def _haystack(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()


def _matches(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]


def classify_domains(*texts: str | None, allowed: list[str] | None = None) -> list[str]:
    """Return the editorial domains a candidate plausibly belongs to."""
    text = _haystack(*texts)
    if not text:
        return []
    hits: dict[str, int] = {}
    for domain, terms in DOMAIN_TERMS.items():
        matched = _matches(text, terms)
        if matched:
            hits[domain] = len(matched)
    if allowed is not None:
        hits = {d: n for d, n in hits.items() if d in allowed}
    # Keep the strongest few; a candidate tagged with nine domains is tagged
    # with none of them usefully.
    ranked = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
    return [domain for domain, _ in ranked[:4]]


def era_from_text(*texts: str | None) -> list[str]:
    text = _haystack(*texts)
    eras: set[str] = set()
    for match in _DECADE.finditer(text):
        eras.add(f"{match.group(1)}s")
    for match in _YEAR.finditer(text):
        year = int(match.group(1))
        eras.add("pre_1900" if year < 1900 else f"{year // 10 * 10}s")
    return sorted(eras)


def detect_music(*texts: str | None, config: MusicConfig | None = None) -> MusicMeta | None:
    """Build :class:`MusicMeta` when a candidate looks musical, else ``None``."""
    text = _haystack(*texts)
    if not text or not _matches(text, MUSIC_TERMS):
        return None

    subgenres = [genre for genre, terms in GENRE_TERMS.items() if _matches(text, terms)]
    parents: list[str] = []
    if config is not None:
        for sub in subgenres:
            parent = config.parent_genre(sub)
            if parent and parent not in parents:
                parents.append(parent)
    subjects = [
        subject for subject, terms in MUSIC_SUBJECT_TERMS.items() if _matches(text, terms)
    ]
    countries = [region for region, terms in REGION_TERMS.items() if _matches(text, terms)]

    return MusicMeta(
        genre=parents,
        subgenre=sorted(set(subgenres)),
        era=era_from_text(text),
        country=sorted(set(countries)),
        subjects=sorted(set(subjects)),
    )


def keywords_from_text(*texts: str | None, limit: int = 18) -> list[str]:
    """Content words, longest-first, for dedup and rotation heuristics."""
    text = _haystack(*texts)
    words = re.findall(r"\b[a-z][a-z'-]{3,}\b", text)
    counts: dict[str, int] = {}
    for word in words:
        if word in _STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [word for word, _ in ranked[:limit]]


def entities_from_text(*texts: str | None, limit: int = 12) -> list[str]:
    """Crude proper-noun extraction. Good enough for cooldowns; the research
    stage replaces it with the model's structured entity list."""
    raw = " ".join(p for p in texts if p)
    candidates = re.findall(r"\b(?:[A-Z][\w'’-]+(?:\s+(?:of|de|van|von|the)\s+)?){1,4}", raw)
    seen: dict[str, None] = {}
    for phrase in candidates:
        cleaned = phrase.strip(" .,;:-")
        if len(cleaned) < 4 or cleaned.lower() in _STOPWORDS:
            continue
        if cleaned.isupper() and len(cleaned) > 8:
            continue
        seen.setdefault(cleaned, None)
    return list(seen)[:limit]


_STOPWORDS = frozenset("""
about above after again against also among another around because been before
being below between both cannot could does doing done during each either else
enough even ever every from further given have having here however into itself
just like made make many more most much must never only other over same should
since some such than that their them then there these they thing this those
through under until upon very were what when where which while with within
without would your file image jpg jpeg png photo photograph picture view
original full size version wikimedia commons category creative license
""".split())


__all__ = [
    "DOMAIN_TERMS",
    "GENRE_TERMS",
    "MUSIC_SUBJECT_TERMS",
    "MUSIC_TERMS",
    "REGION_TERMS",
    "classify_domains",
    "detect_music",
    "entities_from_text",
    "era_from_text",
    "keywords_from_text",
]
