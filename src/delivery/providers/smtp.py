"""SMTP - the recommended default.

Why SMTP first, for this use case: it is free, it needs no account with
anybody new, it works with the mailbox you already have, and for a newsletter
with one subscriber there is no deliverability problem to solve. A Gmail App
Password and four environment variables is the whole setup.

Its limits are real and worth knowing: no delivery webhooks, no bounce
handling, and Gmail's daily send caps. All irrelevant at one email every two
days, all reasons to switch to Resend the moment there is a second reader.

``smtplib`` is synchronous, so the send runs in a worker thread to keep the
event loop free.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, make_msgid

from ...logging_setup import Stage, get_logger, stage
from ..base import (
    EmailConfigurationError,
    EmailMessage,
    EmailProvider,
    SendResult,
    parse_address,
    validate_address,
    validate_message,
)

logger = get_logger(__name__)


class SmtpProvider(EmailProvider):
    name = "smtp"

    def __init__(self, credentials: dict[str, str]) -> None:
        self.host = credentials.get("smtp_host", "")
        self.port = int(credentials.get("smtp_port", "587") or 587)
        self.username = credentials.get("smtp_username", "")
        self.password = credentials.get("smtp_password", "")
        self.security = (credentials.get("smtp_security") or "starttls").lower()
        if not self.host:
            raise EmailConfigurationError("SMTP_HOST is not set")
        if self.security not in ("starttls", "ssl", "none"):
            raise EmailConfigurationError(
                f"SMTP_SECURITY must be starttls, ssl or none (got {self.security!r})"
            )
        if self.security == "none" and self.password:
            logger.warning("SMTP_SECURITY=none sends your password in the clear")

    def _build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        display, address = parse_address(message.sender)
        mime["From"] = formataddr((display, address)) if display else address
        mime["To"] = ", ".join(message.recipients)
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=True)
        mime["Message-ID"] = make_msgid(domain=address.split("@")[-1])
        if message.reply_to:
            mime["Reply-To"] = validate_address(message.reply_to)
        for key, value in message.headers.items():
            mime[key] = value
        mime.set_content(message.text or "This edition is best viewed as HTML.")
        mime.add_alternative(message.html, subtype="html")
        return mime

    def _send_blocking(self, mime: MimeMessage, recipients: list[str],
                       sender: str) -> str:
        context = ssl.create_default_context()
        if self.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=45,
                                                    context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=45)
        try:
            server.ehlo()
            if self.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if self.username:
                server.login(self.username, self.password)
            server.send_message(mime, from_addr=sender, to_addrs=recipients)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - quit failures are cosmetic
                server.close()
        return str(mime["Message-ID"])

    async def send(self, message: EmailMessage) -> SendResult:
        with stage(Stage.EMAIL):
            validate_message(message)
            mime = self._build(message)
            _, sender = parse_address(message.sender)
            try:
                message_id = await asyncio.to_thread(
                    self._send_blocking, mime, message.recipients, sender
                )
            except smtplib.SMTPAuthenticationError as exc:
                return SendResult(False, self.name, detail=(
                    f"authentication rejected by {self.host}: {exc.smtp_error.decode(errors='replace') if isinstance(exc.smtp_error, bytes) else exc}. "
                    "For Gmail you must use an App Password, not your account password."
                ), recipients=message.recipients)
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                return SendResult(False, self.name,
                                  detail=f"{type(exc).__name__}: {exc}",
                                  recipients=message.recipients)
            logger.info("email sent", provider=self.name, host=self.host,
                        recipients=len(message.recipients))
            return SendResult(True, self.name, message_id=message_id,
                              detail=f"delivered via {self.host}",
                              recipients=message.recipients)

    async def verify(self) -> tuple[bool, str]:
        """Open a connection and authenticate without sending anything."""
        def check() -> tuple[bool, str]:
            context = ssl.create_default_context()
            try:
                if self.security == "ssl":
                    server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port,
                                                            timeout=20, context=context)
                else:
                    server = smtplib.SMTP(self.host, self.port, timeout=20)
                with server:
                    server.ehlo()
                    if self.security == "starttls":
                        server.starttls(context=context)
                        server.ehlo()
                    if self.username:
                        server.login(self.username, self.password)
                return True, f"authenticated against {self.host}:{self.port}"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return False, f"{type(exc).__name__}: {exc}"

        return await asyncio.to_thread(check)


__all__ = ["SmtpProvider"]
