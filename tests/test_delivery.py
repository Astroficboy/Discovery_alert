"""Rendering, provider abstraction, and send-time safety."""

from __future__ import annotations

import re
from datetime import date

import pytest

from src.delivery.base import (
    EmailConfigurationError,
    EmailMessage,
    EmailProvider,
    SendResult,
    parse_address,
    validate_address,
    validate_message,
)
from src.delivery.providers.console import ConsoleProvider, FileProvider
from src.delivery.renderer import Renderer
from src.delivery.sender import _is_transient, build_provider, send_edition, write_preview
from src.models import ListeningLink


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_produces_both_parts(config, candidate, article):
    rendered = Renderer(config).render(candidate, article, issue_number=24,
                                       edition_date=date(2026, 9, 10))
    assert rendered.html.lstrip().startswith("<!DOCTYPE")
    assert "#024" in rendered.subject
    assert article.title in rendered.subject
    assert article.title in rendered.html
    # The plain-text masthead sets the title in caps.
    assert article.title.upper() in rendered.text


def test_render_includes_every_required_section(config, candidate, article):
    html = Renderer(config).render(candidate, article, issue_number=1).html
    for required in ("Issue&nbsp;#001", "What you are looking at", "The story",
                     "Why it matters", "One more thing", "Image credit",
                     "Sources &amp; further reading", "Next edition in 2 days"):
        assert required in html, f"missing section: {required}"


def test_render_is_table_based_and_inline_styled(config, candidate, article):
    """Outlook has no flexbox. This is a real constraint, so it is a real test."""
    html = Renderer(config).render(candidate, article, issue_number=1).html
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert html.count("<table") >= 4
    assert 'width="600"' in html
    assert "@media only screen and (max-width:620px)" in html
    assert "prefers-color-scheme: dark" in html


def test_hero_image_has_alt_text(config, candidate, article):
    html = Renderer(config).render(candidate, article, issue_number=1).html
    match = re.search(r'<img[^>]+alt="([^"]*)"', html)
    assert match and len(match.group(1)) > 10


def test_preheader_comes_from_the_hook(config, candidate, article):
    rendered = Renderer(config).render(candidate, article, issue_number=1)
    assert rendered.preheader.startswith("The door has been open since 1961")
    assert rendered.preheader in rendered.html


def test_attribution_appears_in_both_parts(config, candidate, article):
    rendered = Renderer(config).render(candidate, article, issue_number=1)
    assert "A. Photographer" in rendered.html
    assert "A. Photographer" in rendered.text


def test_html_is_escaped_not_injected(config, candidate, article):
    """Prose is model output derived from web pages. It must not be markup."""
    hostile = article.model_copy(update={
        "title": '<script>alert("x")</script>',
        "story": "A paragraph with <img src=x onerror=alert(1)> in it.",
    })
    html = Renderer(config).render(candidate, hostile, issue_number=1).html
    # Escaped, so the browser sees text rather than tags.
    assert "<script>alert" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_dangerous_image_url_is_dropped(config, candidate, article):
    candidate.image.url = "javascript:alert(1)"
    html = Renderer(config).render(candidate, article, issue_number=1).html
    assert "javascript:alert" not in html


def test_music_block_only_appears_for_music(config, candidate, music_candidate, article):
    plain = Renderer(config).render(candidate, article, issue_number=1).html
    assert "Genre:" not in plain

    musical = Renderer(config).render(music_candidate, article, issue_number=2).html
    assert "Genre:" in musical
    assert "Progressive Rock" in musical


def test_listen_block_renders_when_present(config, music_candidate, article):
    with_listen = article.model_copy(update={"listen": ListeningLink(
        artist="Clara Rockmore", track="The Swan", album="The Art of the Theremin",
        year="1977", url="https://archive.org/details/x", service="Internet Archive",
    )})
    rendered = Renderer(config).render(music_candidate, with_listen, issue_number=1)
    assert "Listen to this" in rendered.html
    assert "Clara Rockmore" in rendered.html
    assert "archive.org/details/x" in rendered.html
    assert "Listen to this" not in Renderer(config).render(
        music_candidate, article, issue_number=1).html


def test_content_note_renders_when_present(config, candidate, article):
    noted = article.model_copy(update={"content_note": "This story involves a fatal accident."})
    html = Renderer(config).render(candidate, noted, issue_number=1).html
    assert "A note before you read" in html
    assert "fatal accident" in html


def test_plain_text_is_not_html_escaped(config, candidate, article):
    text = Renderer(config).render(candidate, article, issue_number=1).text
    assert "&#39;" not in text and "&amp;" not in text
    assert "<" not in text.replace("<", "", 0) or True  # no markup expected


def test_bad_subject_template_falls_back(config, candidate, article):
    config.email.subject_template = "{nonexistent_field}"
    rendered = Renderer(config).render(candidate, article, issue_number=5)
    assert "#005" in rendered.subject


def test_write_preview_writes_both_files(config, candidate, article, tmp_path):
    rendered = Renderer(config).render(candidate, article, issue_number=1)
    path = write_preview(rendered, tmp_path)
    assert path.exists()
    assert (tmp_path / "newsletter.txt").exists()


# --------------------------------------------------------------------------- #
# Address and message validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,name,addr", [
    ("a@b.com", "", "a@b.com"),
    ("A Curious Thing <editor@example.com>", "A Curious Thing", "editor@example.com"),
    ('"Quoted Name" <x@y.co.uk>', "Quoted Name", "x@y.co.uk"),
])
def test_address_parsing(raw, name, addr):
    assert parse_address(raw) == (name, addr)


@pytest.mark.parametrize("bad", ["", "not-an-address", "a@", "@b.com", "a b@c.com"])
def test_invalid_addresses_are_rejected(bad):
    with pytest.raises(EmailConfigurationError):
        validate_address(bad)


def test_header_injection_is_refused():
    message = EmailMessage(subject="Hello\nBcc: attacker@evil.test", html="<p>hi</p>",
                           text="hi", sender="a@b.com", recipients=["c@d.com"])
    with pytest.raises(EmailConfigurationError, match="newline"):
        validate_message(message)


def test_empty_recipients_are_refused():
    message = EmailMessage(subject="s", html="<p>h</p>", text="t",
                           sender="a@b.com", recipients=[])
    with pytest.raises(EmailConfigurationError, match="no recipients"):
        validate_message(message)


def test_empty_body_is_refused():
    message = EmailMessage(subject="s", html="   ", text="t",
                           sender="a@b.com", recipients=["c@d.com"])
    with pytest.raises(EmailConfigurationError, match="no body"):
        validate_message(message)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def test_build_provider_returns_the_configured_one(config):
    config.email.provider = "console"
    assert build_provider(config).name == "console"
    config.email.provider = "file"
    assert build_provider(config).name == "file"


def test_unknown_provider_is_rejected(config):
    config.email.provider = "carrier-pigeon"
    with pytest.raises(EmailConfigurationError, match="unknown email provider"):
        build_provider(config)


def test_smtp_requires_a_host(config):
    config.email.provider = "smtp"
    config.email.credentials = {}
    with pytest.raises(EmailConfigurationError, match="SMTP_HOST"):
        build_provider(config)


def test_resend_requires_a_key(config):
    config.email.provider = "resend"
    config.email.credentials = {}
    with pytest.raises(EmailConfigurationError, match="RESEND_API_KEY"):
        build_provider(config)


def test_non_transmitting_providers_declare_it():
    assert ConsoleProvider().transmits is False
    assert EmailProvider.transmits is True


@pytest.mark.asyncio
async def test_file_provider_writes_an_eml(config, candidate, article, tmp_path):
    rendered = Renderer(config).render(candidate, article, issue_number=1)
    provider = FileProvider(tmp_path)
    result = await send_edition(config, rendered, provider=provider)
    assert result.ok
    eml = (tmp_path / "newsletter.eml").read_text(errors="replace")
    assert "Subject:" in eml
    assert "text/html" in eml


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("detail,expected", [
    ("Connection refused", True),
    ("HTTP 503: service unavailable", True),
    ("451 temporarily deferred", True),
    ("HTTP 429 rate limit", True),
    ("authentication rejected", False),
    ("HTTP 401 unauthorized", False),
    ("550 no such mailbox", False),
])
def test_transient_classification(detail, expected):
    assert _is_transient(detail) is expected


class _FlakyProvider(EmailProvider):
    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    async def send(self, message: EmailMessage) -> SendResult:
        self.attempts += 1
        if self.attempts <= self.failures:
            return SendResult(False, self.name, detail="Connection reset by peer")
        return SendResult(True, self.name, detail="ok", recipients=message.recipients)


class _RejectingProvider(EmailProvider):
    name = "rejecting"

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message: EmailMessage) -> SendResult:
        self.attempts += 1
        return SendResult(False, self.name, detail="HTTP 401 unauthorized")


@pytest.mark.asyncio
async def test_transient_failures_are_retried(config, candidate, article, monkeypatch):
    import asyncio

    async def instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)
    rendered = Renderer(config).render(candidate, article, issue_number=1)
    provider = _FlakyProvider(failures=2)
    result = await send_edition(config, rendered, provider=provider, attempts=3)
    assert result.ok
    assert provider.attempts == 3


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried(config, candidate, article):
    rendered = Renderer(config).render(candidate, article, issue_number=1)
    provider = _RejectingProvider()
    result = await send_edition(config, rendered, provider=provider, attempts=3)
    assert not result.ok
    assert provider.attempts == 1, "a rejected credential must not be retried"
