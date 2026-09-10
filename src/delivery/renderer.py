"""Rendering an edition to HTML and plain text.

Autoescaping is on, and every value that reaches the template has already
been through :mod:`src.sanitize`. That matters more than usual here: the
prose is model output derived from web pages, and the image URLs come from
third-party archives. Neither is trusted to be markup-safe.

Two small filters do the typographic work that makes the result look edited
rather than generated: paragraph splitting, and curly quotes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

from ..config import Config
from ..logging_setup import Stage, get_logger, stage
from ..models import Article, Candidate
from ..sanitize import clean_text

logger = get_logger(__name__)

#: Warm paper, dark ink. Overridable per-deployment by editing this dict.
THEME = {
    "canvas": "#efece4",
    "card": "#fffdf8",
    "panel": "#f4f1e8",
    "ink": "#1c1a17",
    "ink_soft": "#6d675c",
    "rule": "#d9d3c6",
    "rule_strong": "#1c1a17",
    "accent": "#8a6d3b",
    "link": "#7a5c2e",
}

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


@dataclass
class RenderedEdition:
    subject: str
    html: str
    text: str
    preheader: str
    issue_number: int
    edition_date: date


class Renderer:
    def __init__(self, config: Config, template_dir: Path | None = None) -> None:
        self.config = config
        self.template_dir = template_dir or (config.repo_root / "templates")
        loader = FileSystemLoader(str(self.template_dir))
        # Autoescaping is per-extension: .html must escape, .txt must not -
        # escaping the plain-text part turns every apostrophe into &#39;.
        self.env = Environment(
            loader=loader,
            autoescape=select_autoescape(
                enabled_extensions=("html", "xhtml", "xml"),
                default_for_string=True,
                default=False,
            ),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["smartquotes"] = _smartquotes
        self.env.globals["paragraphs"] = self._paragraphs
        self.env.globals["section_heading"] = self._section_heading

    # -- template helpers ----------------------------------------------- #
    def _paragraphs(self, text: str) -> Markup:
        """Render a prose block as a run of table rows, one per paragraph."""
        theme = THEME
        rows = []
        for chunk in _PARAGRAPH_SPLIT.split(text or ""):
            paragraph = chunk.strip()
            if not paragraph:
                continue
            rows.append(
                '<tr><td class="ct-pad" style="padding:18px 44px 0 44px;">'
                f'<p class="ct-story ct-ink" style="margin:0; '
                f"font-family:Georgia,'Times New Roman',serif; font-size:17.5px; "
                f'line-height:1.72; color:{theme["ink"]};">'
                f"{escape(_smartquotes(paragraph))}</p></td></tr>"
            )
        # Safe: the only interpolated variable is the paragraph text, and it
        # has been through markupsafe.escape immediately above. Everything else
        # is a literal from this file.
        return Markup("\n".join(rows))  # noqa: S704

    def _section_heading(self, label: str) -> Markup:
        theme = THEME
        # Safe for the same reason: `label` is escaped, the rest is literal.
        return Markup(  # noqa: S704
            '<tr><td class="ct-pad" style="padding:34px 44px 0 44px;">'
            f'<div class="ct-ink-soft" style="font-family:-apple-system,'
            f'BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif; '
            f"font-size:11px; letter-spacing:1.8px; text-transform:uppercase; "
            f'color:{theme["ink_soft"]};">{escape(label)}</div></td></tr>'
        )

    # -- rendering ------------------------------------------------------ #
    def render(self, candidate: Candidate, article: Article, *, issue_number: int,
               edition_date: date | None = None,
               footer_note: str | None = None) -> RenderedEdition:
        with stage(Stage.RENDER):
            edition_date = edition_date or date.today()
            context = self._context(candidate, article, issue_number, edition_date, footer_note)

            html = self.env.get_template("newsletter.html").render(**context)
            text = ""
            if self.config.email.include_plain_text:
                text = self.env.get_template("newsletter.txt").render(**context)

            subject = self._subject(article, issue_number)
            logger.info("edition rendered", issue=issue_number, html_bytes=len(html),
                        text_bytes=len(text))
            return RenderedEdition(
                subject=subject,
                html=html,
                text=text or _fallback_text(article),
                preheader=context["preheader"],
                issue_number=issue_number,
                edition_date=edition_date,
            )

    # ------------------------------------------------------------------ #
    def _context(self, candidate: Candidate, article: Article, issue_number: int,
                 edition_date: date, footer_note: str | None) -> dict:
        newsletter = self.config.newsletter
        image = candidate.image
        return {
            "theme": THEME,
            "newsletter_name": newsletter.name,
            "tagline": newsletter.tagline,
            "issue_number": issue_number,
            "edition_date": edition_date,
            "edition_date_display": edition_date.strftime("%-d %B %Y")
            if hasattr(edition_date, "strftime") else str(edition_date),
            "category_label": self._category_label(candidate),
            "article": _ArticleView(article),
            "image": _ImageView(image),
            "image_alt": self._alt_text(candidate, article),
            "image_caption": clean_text(image.description or "", max_chars=320) or None,
            "image_credit": image.credit or "",
            "music_facts": self._music_facts(candidate),
            "preheader": self._preheader(article),
            "sign_off": newsletter.sign_off.format(days=newsletter.frequency_days),
            "footer_note": footer_note,
        }

    def _subject(self, article: Article, issue_number: int) -> str:
        template = self.config.email.subject_template
        try:
            subject = template.format(
                name=self.config.newsletter.name,
                issue=issue_number,
                title=article.title,
                date=date.today().isoformat(),
            )
        except (KeyError, ValueError, IndexError) as exc:
            logger.warning("bad email.subject_template; using a default", error=str(exc))
            subject = f"{self.config.newsletter.name} #{issue_number:03d} - {article.title}"
        # Inbox previews truncate hard; keep the interesting half.
        return clean_text(subject, max_chars=120)

    def _preheader(self, article: Article) -> str:
        if not self.config.email.preheader_from_hook:
            return self.config.newsletter.tagline
        first = _PARAGRAPH_SPLIT.split(article.hook)[0].strip()
        sentence = re.split(r"(?<=[.!?])\s+", first)[0] if first else ""
        return clean_text(sentence or article.subtitle or self.config.newsletter.tagline,
                          max_chars=140)

    @staticmethod
    def _alt_text(candidate: Candidate, article: Article) -> str:
        base = candidate.image.description or candidate.image.title or article.title
        return clean_text(base, max_chars=220)

    @staticmethod
    def _category_label(candidate: Candidate) -> str:
        if not candidate.categories:
            return ""
        labels = [c.replace("_", " ").title() for c in candidate.categories[:3]]
        return " + ".join(labels)

    def _music_facts(self, candidate: Candidate) -> list[tuple[str, str]]:
        if not candidate.is_music or not candidate.music:
            return []
        music = candidate.music
        facts: list[tuple[str, str]] = []
        genres = music.subgenre or music.genre
        if genres:
            facts.append(("Genre", ", ".join(g.replace("_", " ").title() for g in genres[:3])))
        if music.era:
            facts.append(("Era", ", ".join(music.era[:2])))
        if music.country:
            facts.append(("Place", ", ".join(c.replace("_", " ").title()
                                             for c in music.country[:2])))
        if music.subjects:
            facts.append(("Subject", ", ".join(s.replace("_", " ").title()
                                               for s in music.subjects[:3])))
        return facts


# --------------------------------------------------------------------------- #
# Thin view wrappers - keep template access simple and attribute-based.
# --------------------------------------------------------------------------- #
class _ArticleView:
    def __init__(self, article: Article) -> None:
        self._article = article

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._article, name)


class _ImageView:
    def __init__(self, image) -> None:  # noqa: ANN001
        self._image = image

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._image, name)

    @property
    def url(self) -> str:
        """Only http(s) URLs reach the template. A ``javascript:`` or
        ``data:`` URL in an ``img src`` is not a hypothetical."""
        raw = self._image.url or ""
        return raw if urlparse(raw).scheme in ("http", "https") else ""


_QUOTES = (
    (re.compile(r'(?<=\w)"'), "”"),
    (re.compile(r'"'), "“"),
    (re.compile(r"(?<=\w)'(?=\w)"), "’"),
    (re.compile(r"(?<=\w)'"), "’"),
    (re.compile(r"'"), "‘"),
    (re.compile(r"(?<=\w)\s+--\s+(?=\w)"), " — "),
)


def _smartquotes(text: str) -> str:
    for pattern, replacement in _QUOTES:
        text = pattern.sub(replacement, text)
    return text


def _fallback_text(article: Article) -> str:
    return f"{article.title}\n\n{article.body_text}"


__all__ = ["RenderedEdition", "Renderer", "THEME"]
