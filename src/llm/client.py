"""Provider factory.

``build_llm_client(config)`` is the only place in the application that knows
which providers exist. Everything downstream holds an :class:`LLMClient`.
"""

from __future__ import annotations

from ..config import Config
from ..logging_setup import get_logger
from .base import LLMClient, LLMError
from .providers.anthropic import AnthropicClient
from .providers.openai import OpenAIClient
from .providers.stub import StubClient

logger = get_logger(__name__)

_PROVIDERS: dict[str, type[LLMClient]] = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "stub": StubClient,
}


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def register_provider(name: str, cls: type[LLMClient]) -> None:
    """Hook for a provider that lives outside this repository."""
    _PROVIDERS[name] = cls


def build_llm_client(config: Config) -> LLMClient:
    settings = config.llm
    cls = _PROVIDERS.get(settings.provider)
    if cls is None:
        raise LLMError(
            f"unknown LLM provider {settings.provider!r}; "
            f"available: {', '.join(available_providers())}"
        )
    client = cls(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=float(settings.timeout_seconds),
        max_retries=settings.max_retries,
        max_output_tokens=settings.max_output_tokens,
    )
    if config.llm_fell_back_to_stub:
        logger.warning(
            "no LLM_API_KEY found - falling back to the offline stub writer. "
            "The result will not pass the quality gate; set LLM_API_KEY for real copy."
        )
    return client


__all__ = ["available_providers", "build_llm_client", "register_provider"]
