"""OpenAI-compatible Chat Completions.

Deliberately written against the *shape* rather than one vendor: point
``LLM_BASE_URL`` at any service that speaks Chat Completions - OpenAI,
Together, Groq, OpenRouter, a local llama.cpp server - and it works. That is
most of the value of having an abstraction at all.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from ...logging_setup import get_logger
from ..base import LLMClient, LLMError, LLMResponse, Message

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not self.api_key:
            raise LLMError("openai provider requires LLM_API_KEY", provider=self.name)
        self._client = httpx.AsyncClient(
            base_url=(self.base_url or DEFAULT_BASE_URL).rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
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
            "messages": [
                {"role": "system", "content": system},
                *({"role": m.role, "content": m.content} for m in messages),
            ],
        }
        last_error = "unknown error"
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return _parse(response.json(), model)
                last_error = _error_message(response)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise LLMError(f"openai: {last_error}", provider=self.name)
            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt, 20) * (1 + random.uniform(-0.2, 0.2)))  # noqa: S311
        raise LLMError(f"openai: giving up after {self.max_retries} attempts ({last_error})",
                       provider=self.name, retryable=True)


def _parse(body: dict[str, Any], model: str) -> LLMResponse:
    choices = body.get("choices") or []
    text = ""
    stop_reason = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content")) or ""
        stop_reason = choices[0].get("finish_reason", "")
    usage = body.get("usage") or {}
    return LLMResponse(
        text=text,
        model=body.get("model", model),
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        stop_reason=stop_reason,
        raw=body,
    )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = (body.get("error") or {}).get("message") or body.get("message")
    except ValueError:
        detail = response.text[:300]
    return f"HTTP {response.status_code}: {detail}"


__all__ = ["OpenAIClient"]
