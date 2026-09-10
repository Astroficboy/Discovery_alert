"""Resend.

The provider to move to when SMTP stops being enough: a real API, delivery
logs, bounce handling, and a free tier that comfortably covers a personal
newsletter. Requires a verified sending domain.
"""

from __future__ import annotations

import httpx

from ...logging_setup import Stage, get_logger, stage
from ..base import (
    EmailConfigurationError,
    EmailMessage,
    EmailProvider,
    SendResult,
    validate_message,
)

logger = get_logger(__name__)

API_URL = "https://api.resend.com/emails"


class ResendProvider(EmailProvider):
    name = "resend"

    def __init__(self, credentials: dict[str, str]) -> None:
        self.api_key = credentials.get("resend_api_key", "")
        if not self.api_key:
            raise EmailConfigurationError("RESEND_API_KEY is not set")
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
            payload = {
                "from": message.sender,
                "to": message.recipients,
                "subject": message.subject,
                "html": message.html,
                "text": message.text,
            }
            if message.reply_to:
                payload["reply_to"] = message.reply_to
            try:
                response = await self._client.post(API_URL, json=payload)
            except httpx.HTTPError as exc:
                return SendResult(False, self.name, detail=f"{type(exc).__name__}: {exc}",
                                  recipients=message.recipients)
            if response.status_code >= 400:
                return SendResult(False, self.name,
                                  detail=f"HTTP {response.status_code}: {response.text[:300]}",
                                  recipients=message.recipients)
            body = response.json() if response.content else {}
            logger.info("email sent", provider=self.name, recipients=len(message.recipients))
            return SendResult(True, self.name, message_id=body.get("id"),
                              detail="accepted by Resend", recipients=message.recipients)

    async def verify(self) -> tuple[bool, str]:
        try:
            response = await self._client.get("https://api.resend.com/domains")
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if response.status_code == 401:
            return False, "RESEND_API_KEY was rejected"
        return response.status_code < 400, f"HTTP {response.status_code}"


__all__ = ["ResendProvider"]
