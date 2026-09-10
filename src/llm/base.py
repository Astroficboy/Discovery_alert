"""The LLM contract.

One method - :meth:`LLMClient.complete` - plus a JSON convenience wrapper.
Everything the application does with a model goes through it, so adding a
provider is a single subclass and a registry entry, and the rest of the code
never learns which model it is talking to.

Two rules are enforced here rather than left to each caller:

* **System instructions and source material are separate arguments.** A
  provider implementation may not concatenate untrusted content into the
  system prompt.
* **Retries are the client's job.** Callers get either a response or an
  :class:`LLMError`, never a half-finished stream.
"""

from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..logging_setup import Stage, get_logger, stage

logger = get_logger(__name__)

Role = Literal["user", "assistant"]


class LLMError(RuntimeError):
    """Any failure to obtain a usable completion."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class UsageLedger:
    """Running token count for one pipeline run, so a dry run can print what
    a real run would have cost."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_purpose: dict[str, int] = field(default_factory=dict)

    def record(self, response: LLMResponse, purpose: str) -> None:
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + response.total_tokens

    def summary(self) -> str:
        return (f"{self.calls} calls, {self.input_tokens:,} in / "
                f"{self.output_tokens:,} out tokens")


class LLMClient(abc.ABC):
    """Base class for every provider."""

    name: str = "unnamed"

    def __init__(self, *, model: str, api_key: str | None = None,
                 base_url: str | None = None, timeout: float = 120.0,
                 max_retries: int = 3, max_output_tokens: int = 4096) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.usage = UsageLedger()

    @abc.abstractmethod
    async def _complete(self, *, system: str, messages: list[Message], model: str,
                        temperature: float, max_tokens: int,
                        purpose: str) -> LLMResponse:
        """Provider-specific call. Raise :class:`LLMError` on failure."""

    async def complete(self, *, system: str, user: str, purpose: str = "generic",
                       temperature: float = 0.7, model: str | None = None,
                       max_tokens: int | None = None,
                       prefill: str | None = None) -> LLMResponse:
        """One turn. ``prefill`` seeds the assistant's reply, which is how we
        force well-formed JSON without a schema-constrained decoding API."""
        messages = [Message("user", user)]
        if prefill:
            messages.append(Message("assistant", prefill))
        with stage(Stage.LLM):
            response = await self._complete(
                system=system,
                messages=messages,
                model=model or self.model,
                temperature=temperature,
                max_tokens=max_tokens or self.max_output_tokens,
                purpose=purpose,
            )
            if prefill:
                response.text = prefill + response.text
            self.usage.record(response, purpose)
            logger.debug("completion", purpose=purpose, model=response.model,
                         in_tokens=response.input_tokens, out_tokens=response.output_tokens)
            return response

    async def complete_json(self, *, system: str, user: str, purpose: str = "generic",
                            temperature: float = 0.1, model: str | None = None,
                            max_tokens: int | None = None,
                            expect: type = dict) -> Any:
        """Ask for JSON and return parsed data, retrying once on malformed
        output with the parser error fed back to the model."""
        prefill = "[" if expect is list else "{"
        response = await self.complete(
            system=system, user=user, purpose=purpose, temperature=temperature,
            model=model, max_tokens=max_tokens, prefill=prefill,
        )
        try:
            return parse_json(response.text, expect=expect)
        except ValueError as first_error:
            logger.warning("model returned malformed JSON; retrying once",
                           purpose=purpose, error=str(first_error))
            retry = await self.complete(
                system=system,
                user=(f"{user}\n\n[Your previous reply could not be parsed: "
                      f"{first_error}. Reply with valid JSON only - no prose, no code "
                      f"fences, no trailing commas.]"),
                purpose=f"{purpose}_retry", temperature=0.0, model=model,
                max_tokens=max_tokens, prefill=prefill,
            )
            try:
                return parse_json(retry.text, expect=expect)
            except ValueError as second_error:
                raise LLMError(f"{purpose}: model did not return usable JSON "
                               f"({second_error})", provider=self.name) from second_error

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not required
        """Release provider resources. Safe to call more than once.

        Deliberately concrete rather than abstract: a provider with nothing to
        close (the stub, for instance) should not have to write an empty
        method to satisfy the interface.
        """


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json(text: str, *, expect: type = dict) -> Any:
    """Parse JSON out of a model reply, tolerating fences and trailing prose."""
    cleaned = _FENCE.sub("", text).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        value = _json_from_substring(cleaned, expect)
    if not isinstance(value, expect):
        raise ValueError(f"expected {expect.__name__}, got {type(value).__name__}")
    return value


def _json_from_substring(text: str, expect: type) -> Any:
    """Find the first balanced JSON value of the expected type."""
    opener, closer = ("[", "]") if expect is list else ("{", "}")
    start = text.find(opener)
    if start == -1:
        raise ValueError("no JSON value found in reply")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"malformed JSON: {exc}") from exc
    raise ValueError("unterminated JSON value in reply")


__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "Message",
    "Role",
    "UsageLedger",
    "parse_json",
]
