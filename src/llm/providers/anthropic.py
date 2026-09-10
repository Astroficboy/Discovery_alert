"""Anthropic Messages API.

Called over plain HTTP with httpx rather than through the SDK: it is one
endpoint, the request shape is stable, and it keeps the dependency list at
five packages. Swapping in the official SDK later means changing this file
and nothing else.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from ...logging_setup import get_logger
from ..base import LLMClient, LLMError, LLMResponse, Message

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not self.api_key:
            raise LLMError("anthropic provider requires LLM_API_KEY", provider=self.name)
        self._client = httpx.AsyncClient(
            base_url=(self.base_url or DEFAULT_BASE_URL).rstrip("/"),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(self.timeout),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _complete(self, *, system: str, messages: list[Message], model: str,
                        temperature: float, max_tokens: int,
                        purpose: str = "generic") -> LLMResponse:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }

        last_error = "unknown error"
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.post("/v1/messages", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return _parse(response.json(), model)
                last_error = _error_message(response)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise LLMError(f"anthropic: {last_error}", provider=self.name)
            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt, 20) * (1 + random.uniform(-0.2, 0.2)))  # noqa: S311
        raise LLMError(f"anthropic: giving up after {self.max_retries} attempts "
                       f"({last_error})", provider=self.name, retryable=True)


def _parse(body: dict[str, Any], model: str) -> LLMResponse:
    blocks = body.get("content") or []
    text = "".join(
        block.get("text", "") for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    usage = body.get("usage") or {}
    return LLMResponse(
        text=text,
        model=body.get("model", model),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        stop_reason=body.get("stop_reason", ""),
        raw=body,
    )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = (body.get("error") or {}).get("message") or body.get("message")
    except ValueError:
        detail = response.text[:300]
    return f"HTTP {response.status_code}: {detail}"


__all__ = ["AnthropicClient"]
