"""Choosing a provider and sending, with retries.

The retry policy here is deliberately different from the one used for
discovery: an email send is not idempotent, so we retry only failures that
are unambiguously "this did not happen" - connection errors, timeouts,
5xx - and never a rejection that might have been a partial delivery.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

from ..config import Config
from ..logging_setup import Stage, get_logger, stage
from .base import EmailConfigurationError, EmailMessage, EmailProvider, SendResult
from .providers.console import ConsoleProvider, FileProvider
from .providers.resend import ResendProvider
from .providers.sendgrid import SendGridProvider
from .providers.smtp import SmtpProvider
from .renderer import RenderedEdition

logger = get_logger(__name__)

_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "temporarily", "try again",
    "http 5", "421", "450", "451", "452", "http 429", "rate limit",
)


def build_provider(config: Config, *, force: str | None = None) -> EmailProvider:
    """Instantiate the configured provider. Raises on misconfiguration."""
    name = (force or config.email.provider).lower()
    credentials = config.email.credentials
    if name == "smtp":
        return SmtpProvider(credentials)
    if name == "resend":
        return ResendProvider(credentials)
    if name == "sendgrid":
        return SendGridProvider(credentials)
    if name == "console":
        return ConsoleProvider()
    if name == "file":
        return FileProvider(config.output_dir)
    raise EmailConfigurationError(
        f"unknown email provider {name!r}; "
        "expected one of: smtp, resend, sendgrid, console, file"
    )


def register_provider_factory(name: str, factory) -> None:  # noqa: ANN001
    """Hook for a provider defined outside this repository."""
    _EXTRA[name] = factory


_EXTRA: dict[str, object] = {}


async def send_edition(config: Config, edition: RenderedEdition, *,
                       provider: EmailProvider | None = None,
                       attempts: int = 3) -> SendResult:
    """Send, retrying only genuinely transient failures."""
    with stage(Stage.EMAIL):
        owns_provider = provider is None
        provider = provider or build_provider(config)
        message = EmailMessage(
            subject=edition.subject,
            html=edition.html,
            text=edition.text,
            sender=config.email.sender,
            recipients=config.email.recipients,
            reply_to=config.email.reply_to,
            headers={
                "X-Curious-Things-Issue": str(edition.issue_number),
                "List-Unsubscribe": f"<mailto:{_bare(config.email.sender)}?subject=unsubscribe>",
            },
        )
        try:
            result = SendResult(False, provider.name, detail="not attempted")
            for attempt in range(1, attempts + 1):
                result = await provider.send(message)
                if result.ok:
                    return result
                if not _is_transient(result.detail) or attempt == attempts:
                    logger.error("email send failed", provider=result.provider,
                                 detail=result.detail, attempt=attempt)
                    return result
                delay = min(2 ** attempt, 30) * (1 + random.uniform(-0.2, 0.2))  # noqa: S311
                logger.warning("transient email failure; retrying",
                               attempt=attempt, delay=round(delay, 1), detail=result.detail)
                await asyncio.sleep(delay)
            return result
        finally:
            if owns_provider:
                await provider.aclose()


def _is_transient(detail: str) -> bool:
    lowered = (detail or "").lower()
    if "authentication" in lowered or "unauthorized" in lowered or "401" in lowered:
        return False
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _bare(address: str) -> str:
    from .base import parse_address

    return parse_address(address)[1]


def write_preview(edition: RenderedEdition, output_dir: Path) -> Path:
    """Write the HTML somewhere a browser can open it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "newsletter.html"
    path.write_text(edition.html, encoding="utf-8")
    (output_dir / "newsletter.txt").write_text(edition.text, encoding="utf-8")
    logger.info("preview written", path=str(path), stage=Stage.RENDER)
    return path


__all__ = ["build_provider", "register_provider_factory", "send_edition", "write_preview"]
