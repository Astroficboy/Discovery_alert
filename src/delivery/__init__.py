"""Delivery: rendering the edition and getting it into an inbox.

Named ``delivery`` rather than ``email`` on purpose. A package called
``src.email`` shadows the standard library's ``email`` package for any tool
that puts ``src/`` on ``sys.path`` - and ``smtplib`` depends on that stdlib
package, so the failure mode is a broken mail sender at 08:00 rather than an
import error at development time. The extra three letters are cheap.
"""

from .base import EmailMessage, EmailProvider, SendResult  # noqa: F401
from .renderer import RenderedEdition, Renderer  # noqa: F401
from .sender import build_provider, send_edition  # noqa: F401

__all__ = [
    "EmailMessage",
    "EmailProvider",
    "RenderedEdition",
    "Renderer",
    "SendResult",
    "build_provider",
    "send_edition",
]
