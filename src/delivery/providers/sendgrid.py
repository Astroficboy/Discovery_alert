"""SendGrid.

Included because it is the provider people most often already have. The API
shape is more baroque than Resend's for no benefit at this scale, but it
works and the free tier is generous.
"""

from __future__ import annotations

import httpx

from ...logging_setup import Stage, get_logger, stage
from ..base import (
    EmailConfigurationError,
    EmailMessage,
    EmailProvider,
    SendResult,
    parse_address,
    validate_message,
)

logger = get_logger(__name__)

API_URL = "https://api.sendgrid.com/v3/mail/send"


class SendGridProvider(EmailProvider):
    name = "sendgrid"

    def __init__(self, credentials: dict[str, str]) -> None:
        self.api_key = credentials.get("sendgrid_api_key", "")
        if not self.api_key:
            raise EmailConfigurationError("SENDGRID_API_KEY is not set")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, message: EmailMessage) -> SendResult:
        with stage(Stage.EMAIL):
            validate_message(message)
            display, address = parse_address(message.sender)
            payload = {
                "personalizations": [
                    {"to": [{"email": parse_address(r)[1]} for r in message.recipients]}
                ],
                "from": {"email": address, **({"name": display} if display else {})},
                "subject": message.subject,
                "content": [
                    {"type": "text/plain", "value": message.text or " "},
                    {"type": "text/html", "value": message.html},
                ],
            }
            if message.reply_to:
                payload["reply_to"] = {"email": parse_address(message.reply_to)[1]}
            try:
                response = await self._client.post(API_URL, json=payload)
            except httpx.HTTPError as exc:
                return SendResult(False, self.name, detail=f"{type(exc).__name__}: {exc}",
                                  recipients=message.recipients)
            if response.status_code >= 400:
                return SendResult(False, self.name,
                                  detail=f"HTTP {response.status_code}: {response.text[:300]}",
                                  recipients=message.recipients)
            logger.info("email sent", provider=self.name, recipients=len(message.recipients))
            return SendResult(True, self.name,
                              message_id=response.headers.get("X-Message-Id"),
                              detail="accepted by SendGrid", recipients=message.recipients)


__all__ = ["SendGridProvider"]
