"""A deterministic, offline "model".

This exists for three reasons, and it earns its place for all three:

* ``run --dry-run`` works on a fresh clone with no API key, which is what the
  acceptance criteria ask for;
* the test suite can exercise the whole pipeline without touching a network;
* when a real provider fails mid-run you can re-run with ``LLM_PROVIDER=stub``
  to establish whether the problem is the model or everything else.

It is not a writer. It returns structurally valid, obviously-placeholder
output, and the quality gate is deliberately *not* rigged to pass it: a stub
edition scores below the threshold and will be refused unless you pass
``--force``. That is the honest behaviour - a newsletter of placeholder prose
is worse than no newsletter.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..base import LLMClient, LLMResponse, Message


class StubClient(LLMClient):
    name = "stub"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("model", "stub-deterministic-1")
        kwargs["api_key"] = kwargs.get("api_key") or "not-required"
        super().__init__(**kwargs)

    async def _complete(self, *, system: str, messages: list[Message], model: str,
                        temperature: float, max_tokens: int,
                        purpose: str = "generic") -> LLMResponse:
        user = messages[0].content if messages else ""
        prefill = messages[1].content if len(messages) > 1 else ""
        text = self._respond(purpose, user, prefill)
        # Rough token accounting so dry-run cost estimates are not zero.
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=len(system + user) // 4,
            output_tokens=len(text) // 4,
            stop_reason="end_turn",
        )

    # -- responses ------------------------------------------------------ #
    def _respond(self, purpose: str, user: str, prefill: str) -> str:
        handler = {
            "triage": self._triage,
            "research_plan": self._research_plan,
            "research_synthesis": self._synthesis,
            "fact_check": self._fact_check,
            "score": self._score,
            "write": self._write,
            "quality": self._quality,
        }.get(purpose.replace("_retry", ""), None)
        if handler is None:
            return self._trim_prefill('{"note": "stub response"}', prefill)
        return self._trim_prefill(handler(user), prefill)

    @staticmethod
    def _trim_prefill(payload: str, prefill: str) -> str:
        """The caller re-attaches the prefill, so do not repeat it."""
        if prefill and payload.startswith(prefill):
            return payload[len(prefill):]
        return payload

    def _ids(self, user: str) -> list[str]:
        return re.findall(r'"candidate_id"\s*:\s*"([A-Za-z0-9_-]+)"', user) or \
               re.findall(r"\[([a-f0-9]{8,24})\]", user)

    def _title(self, user: str) -> str:
        match = re.search(r'"title"\s*:\s*"([^"]{3,120})"', user)
        if match:
            return match.group(1)
        match = re.search(r"^TITLE:\s*(.+)$", user, re.MULTILINE)
        return match.group(1).strip() if match else "An unlabelled photograph"

    def _seed(self, user: str) -> int:
        return int(hashlib.sha256(user.encode("utf-8")).hexdigest()[:8], 16)

    def _triage(self, user: str) -> str:
        ids = self._ids(user)
        verdicts = []
        for index, candidate_id in enumerate(ids):
            interest = 55 + (self._seed(candidate_id) % 40)
            verdicts.append({
                "candidate_id": candidate_id,
                "interest": interest,
                "the_question": "What is actually going on in this photograph?",
                "likely_angle": "Placeholder triage from the offline stub model.",
                "domains": [],
                "verdict": "pursue" if index < 6 else "maybe",
                "reason": "Stub model: interest score is deterministic, not editorial.",
            })
        return json.dumps(verdicts, indent=1)

    def _research_plan(self, user: str) -> str:
        return json.dumps({
            "core_question": f"What is the story behind “{self._title(user)}”?",
            "queries": ["overview", "primary source", "institutional record"],
            "entities": [],
        }, indent=1)

    def _synthesis(self, user: str) -> str:
        title = self._title(user)
        return json.dumps({
            "core_question": f"What is the story behind “{title}”?",
            "summary": ("Offline stub synthesis. No research was performed by a model; "
                        "the source excerpts below were collected by the pipeline itself."),
            "claims": [{
                "text": f"The image is catalogued as “{title}”.",
                "confidence": "established",
                "supporting_urls": [],
                "note": "Recorded from archive metadata, not from a model.",
            }],
            "disagreements": [],
            "entities": [],
            "keywords": [],
            "timeline": [],
            "content_notes": [],
        }, indent=1)

    def _fact_check(self, user: str) -> str:
        return json.dumps({
            "verified": [],
            "unsupported": [],
            "corrections": [],
            "notes": "Stub model performed no verification.",
        }, indent=1)

    def _score(self, user: str) -> str:
        seed = self._seed(user)
        base = 55 + seed % 12
        return json.dumps({
            "visual_score": base,
            "story_score": base - 4,
            "novelty_score": base - 2,
            "significance_score": base,
            "curiosity_score": base - 3,
            "historical_significance": base,
            "scientific_significance": base - 6,
            "emotional_impact": base - 5,
            "music_significance": base - 4,
            "cultural_significance": base - 2,
            "technical_significance": base - 3,
            "genre_interest": base - 5,
            "rationale": "Deterministic stub scoring. Not an editorial judgement.",
            "concerns": ["Scored by the offline stub model, which cannot read the image."],
        }, indent=1)

    def _write(self, user: str) -> str:
        title = self._title(user)
        filler = (
            "This paragraph is placeholder text produced by the offline stub model, "
            "which is used when no LLM API key is configured. It exists so that the "
            "rendering, quality-control and delivery stages of the pipeline can be "
            "exercised end to end without network access. It is not an edition, and "
            "the quality gate is expected to reject it. "
        )
        return json.dumps({
            "title": f"[STUB] {title}",
            "subtitle": "Generated without a language model",
            "hook": ("This edition was generated by the offline stub writer. Configure "
                     "LLM_API_KEY to get real editorial copy."),
            "the_image": filler,
            "story": filler * 3,
            "bigger_picture": filler,
            "one_more_thing": filler,
            "listen": None,
            "content_note": None,
        }, indent=1)

    def _quality(self, user: str) -> str:
        return json.dumps({
            "accuracy": 40,
            "sourcing": 40,
            "writing": 20,
            "image_fit": 50,
            "safety_ok": True,
            "issues": ["Copy was produced by the offline stub model, not a writer."],
            "fixes_requested": ["Configure a real LLM provider."],
            "notes": "Stub quality review: this edition should not be sent.",
        }, indent=1)


__all__ = ["StubClient"]
