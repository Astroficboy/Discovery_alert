"""Shared fixtures.

Nothing here touches the network. External services are represented by
``httpx.MockTransport`` (recorded API shapes) and by :class:`ScriptedLLM`,
which returns whatever the test tells it to for each purpose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.config import Config, load_config
from src.llm.base import LLMClient, LLMResponse, Message
from src.models import (
    Article,
    Candidate,
    Claim,
    ImageAsset,
    LicenseInfo,
    MusicMeta,
    ResearchDossier,
    SourceRef,
)
from src.net import HttpClient
from src.storage.database import Database

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _quiet_logging():
    from src.logging_setup import configure_logging

    configure_logging("ERROR", "text")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = load_config(load_dotenv_file=False)
    cfg.storage.database_path = str(tmp_path / "test.db")
    cfg.repo_root = Path(__file__).resolve().parent.parent
    cfg.output_path = str(tmp_path / "output")
    cfg.email.sender = "A Curious Thing <editor@example.com>"
    cfg.email.recipients = ["reader@example.com"]
    cfg.email.provider = "console"
    return cfg


@pytest.fixture
def database(config: Config) -> Database:
    db = Database(config.database_file)
    yield db
    db.close()


# --------------------------------------------------------------------------- #
# A scripted LLM
# --------------------------------------------------------------------------- #
class ScriptedLLM(LLMClient):
    """Returns a canned reply per purpose, and records what it was asked."""

    name = "scripted"

    def __init__(self, replies: dict[str, Any] | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("model", "scripted-1")
        super().__init__(**kwargs)
        self.replies = replies or {}
        self.calls: list[tuple[str, str, str]] = []  # (purpose, system, user)
        self.fail_purposes: set[str] = set()

    async def _complete(self, *, system: str, messages: list[Message], model: str,
                        temperature: float, max_tokens: int,
                        purpose: str = "generic") -> LLMResponse:
        user = messages[0].content if messages else ""
        prefill = messages[1].content if len(messages) > 1 else ""
        self.calls.append((purpose, system, user))
        if purpose in self.fail_purposes:
            from src.llm.base import LLMError

            raise LLMError(f"scripted failure for {purpose}", provider=self.name)
        payload = self.replies.get(purpose.replace("_retry", ""), {"note": "unscripted"})
        text = payload if isinstance(payload, str) else json.dumps(payload)
        if prefill and text.startswith(prefill):
            text = text[len(prefill):]
        return LLMResponse(text=text, model=model, input_tokens=10, output_tokens=20)


@pytest.fixture
def scripted_llm() -> ScriptedLLM:
    return ScriptedLLM()


# --------------------------------------------------------------------------- #
# Domain objects
# --------------------------------------------------------------------------- #
@pytest.fixture
def image() -> ImageAsset:
    return ImageAsset(
        url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Example.jpg",
        page_url="https://commons.wikimedia.org/wiki/File:Example.jpg",
        width=3000, height=2000, mime_type="image/jpeg",
        title="An example photograph", description="A photograph of something unusual.",
        creator="A. Photographer", created="1961", institution="Example Archive",
        license=LicenseInfo(id="pd", name="Public domain", reusable=True,
                            requires_attribution=False, commercial_ok=True),
        credit="“An example photograph” · A. Photographer · 1961 · Example Archive · Public domain",
    )


@pytest.fixture
def candidate(image: ImageAsset) -> Candidate:
    return Candidate(
        source="wikimedia_potd",
        source_url="https://commons.wikimedia.org/wiki/File:Example.jpg",
        title="The abandoned relay station at Vela",
        description="A rusting relay station, abandoned in 1961 and never dismantled. " * 6,
        image=image,
        categories=["history", "engineering", "technology"],
        keywords=["relay", "abandoned", "station", "1961"],
        entities=["Vela", "Marconi"],
        date_hint="1961",
    )


@pytest.fixture
def music_candidate(image: ImageAsset) -> Candidate:
    return Candidate(
        source="wikimedia_music",
        source_url="https://commons.wikimedia.org/wiki/File:Console.jpg",
        title="An EMI mixing console, 1967",
        description="The eight-track console used at a London studio, built in-house. " * 6,
        image=image.model_copy(update={"url": "https://example.org/console.jpg"}),
        categories=["music", "technology", "history"],
        music=MusicMeta(genre=["rock"], subgenre=["progressive_rock"], era=["1960s"],
                        country=["united_kingdom"], subjects=["recording", "studios"]),
        keywords=["console", "studio", "recording"],
        entities=["EMI", "London"],
    )


@pytest.fixture
def dossier() -> ResearchDossier:
    return ResearchDossier(
        core_question="Why was the station abandoned?",
        summary="A summary of what the sources establish about the station. " * 8,
        claims=[
            Claim(text="The station opened in 1954.", confidence="established",
                  supporting_urls=["https://www.loc.gov/x", "https://www.si.edu/y"]),
            Claim(text="It was abandoned in 1961.", confidence="well_evidenced",
                  supporting_urls=["https://www.loc.gov/x", "https://www.archives.gov/z"]),
            Claim(text="A local story says the last operator never left.",
                  confidence="legend", supporting_urls=["https://en.wikipedia.org/wiki/X"]),
        ],
        sources=[
            SourceRef(title="Station records", url="https://www.loc.gov/x",
                      publisher="Library of Congress", authority=95),
            SourceRef(title="Collection entry", url="https://www.si.edu/y",
                      publisher="Smithsonian Institution", authority=94),
            SourceRef(title="Background", url="https://en.wikipedia.org/wiki/X",
                      publisher="Wikipedia", authority=50),
        ],
        entities=["Vela", "Marconi"],
        keywords=["relay", "abandoned"],
    )


@pytest.fixture
def article() -> Article:
    body = (
        "The station sits four kilometres from the nearest road, and the door has been "
        "open since 1961. Nobody locked it because nobody expected to be the last person "
        "out. "
    )
    return Article(
        title="The relay station nobody bothered to close",
        subtitle="It was left open in 1961, and it is still open",
        hook="The door has been open since 1961. Nobody locked it, because nobody knew "
             "they were the last person out. The equipment inside is still bolted down.",
        the_image="You are looking at a concrete shed on a ridge, photographed in 1961.",
        story="\n\n".join([body * 4, body * 4, body * 4, body * 4]),
        bigger_picture="What the station did mattered more than what it looked like. " * 6,
        one_more_thing="The last logged transmission was a weather report for a ship "
                       "that had already docked.",
        sources=[
            SourceRef(title="Station records", url="https://www.loc.gov/x",
                      publisher="Library of Congress", authority=95),
            SourceRef(title="Collection entry", url="https://www.si.edu/y",
                      publisher="Smithsonian Institution", authority=94),
            SourceRef(title="Background", url="https://en.wikipedia.org/wiki/X",
                      publisher="Wikipedia", authority=50),
        ],
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_candidates(config: Config) -> list[Candidate]:
    """The bundled fixture candidates, gated exactly as discovery would gate them.

    Synchronous: ``BundledFixtures.load`` reads a file and never touches the
    HTTP client, so tests can call this outside an event loop.
    """
    from src.config import SourceConfig
    from src.discovery.fixtures import BundledFixtures

    source = BundledFixtures(config, SourceConfig(name="fixtures", limit=20), None)
    return source.apply_gates(source.load())


def fixture_ids(config: Config) -> list[str]:
    return [candidate.id for candidate in fixture_candidates(config)]


def mock_http(routes: dict[str, Any], *, default_status: int = 404) -> HttpClient:
    """An HttpClient whose transport answers from ``routes``.

    Keys are matched as substrings of the request URL, so a route for
    ``"commons.wikimedia.org"`` catches every Commons call.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for fragment, payload in routes.items():
            if fragment in url:
                if isinstance(payload, httpx.Response):
                    return payload
                if isinstance(payload, (dict, list)):
                    return httpx.Response(200, json=payload)
                return httpx.Response(200, text=str(payload))
        return httpx.Response(default_status, json={"error": "no route", "url": url})

    return HttpClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# asyncio support without an extra plugin dependency
# --------------------------------------------------------------------------- #
def pytest_configure(config):  # noqa: ANN001
    config.addinivalue_line("markers", "asyncio: run this coroutine test in an event loop")


def pytest_pyfunc_call(pyfuncitem):  # noqa: ANN001
    import asyncio
    import inspect

    test = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(test(**kwargs))
    return True
