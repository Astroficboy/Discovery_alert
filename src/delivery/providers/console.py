"""Providers that do not send anything.

``console`` prints a summary; ``file`` writes a ``.eml`` you can open in a
mail client to see exactly what would have arrived. Both are what
``--dry-run`` uses, and both report ``transmits = False`` so the pipeline can
assert that a dry run really did not send.
"""

from __future__ import annotations

import sys
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate
from pathlib import Path

from ...logging_setup import Stage, get_logger, stage
from ..base import EmailMessage, EmailProvider, SendResult, validate_message

logger = get_logger(__name__)


class ConsoleProvider(EmailProvider):
    name = "console"
    transmits = False

    async def send(self, message: EmailMessage) -> SendResult:
        with stage(Stage.EMAIL):
            validate_message(message)
            print("\n" + "=" * 72, file=sys.stdout)
            print(f"To:      {', '.join(message.recipients)}", file=sys.stdout)
            print(f"From:    {message.sender}", file=sys.stdout)
            print(f"Subject: {message.subject}", file=sys.stdout)
            print("=" * 72, file=sys.stdout)
            print(message.text or "(no plain-text part)", file=sys.stdout)
            print("=" * 72 + "\n", file=sys.stdout)
            logger.info("edition printed to stdout; nothing was sent", provider=self.name)
            return SendResult(True, self.name, detail="printed to stdout (not sent)",
                              recipients=message.recipients)


class FileProvider(EmailProvider):
    name = "file"
    transmits = False

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def send(self, message: EmailMessage) -> SendResult:
        with stage(Stage.EMAIL):
            validate_message(message)
            mime = MimeMessage()
            mime["From"] = message.sender
            mime["To"] = ", ".join(message.recipients)
            mime["Subject"] = message.subject
            mime["Date"] = formatdate(localtime=True)
            mime.set_content(message.text or " ")
            mime.add_alternative(message.html, subtype="html")

            path = self.output_dir / "newsletter.eml"
            path.write_bytes(bytes(mime))
            logger.info("edition written to disk; nothing was sent",
                        provider=self.name, path=str(path))
            return SendResult(True, self.name, detail=f"written to {path} (not sent)",
                              recipients=message.recipients)


__all__ = ["ConsoleProvider", "FileProvider"]
