"""The adversarial pass over a dossier.

Separate from the researcher on purpose. The model that assembled the story
has already committed to it; a second call, framed as an audit and given the
same evidence, catches things the first one talked itself into.

What it produces is applied mechanically rather than trusted as prose:
confidence levels get downgraded, unsupported claims get dropped from the
dossier before the writer ever sees them, and the residue is handed to the
scorer as ``concerns``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..llm.base import LLMClient, LLMError
from ..llm.prompts import FACT_CHECK_SYSTEM, fact_check_prompt
from ..logging_setup import Stage, get_logger, stage
from ..models import ResearchDossier
from ..sanitize import UntrustedContent, clean_text, untrusted_block
from .researcher import VALID_CONFIDENCE

logger = get_logger(__name__)

_WEAKER = {"established": 3, "well_evidenced": 2, "plausible": 1, "speculative": 0, "legend": 0}


@dataclass
class FactChecker:
    config: Config
    llm: LLMClient

    async def check(self, dossier: ResearchDossier,
                    blocks: list[UntrustedContent]) -> dict[str, Any]:
        """Audit and mutate ``dossier`` in place. Returns the raw report."""
        with stage(Stage.FACTCHECK):
            if not dossier.claims:
                return {"notes": "no claims to check"}
            try:
                report = await self.llm.complete_json(
                    system=FACT_CHECK_SYSTEM,
                    user=fact_check_prompt(dossier, untrusted_block(blocks)),
                    purpose="fact_check",
                    temperature=self.config.llm.analysis_temperature,
                    max_tokens=2048,
                )
            except LLMError as exc:
                logger.warning("fact check failed; dossier used unchecked", error=str(exc))
                dossier.errors.append(f"fact check failed: {exc}")
                return {"error": str(exc)}

            self.apply(dossier, report)
            logger.info(
                "fact check complete",
                unsupported=len(report.get("unsupported", []) or []),
                corrections=len(report.get("corrections", []) or []),
                downgrades=len(report.get("downgrade_confidence", []) or []),
            )
            return report

    # ------------------------------------------------------------------ #
    @staticmethod
    def apply(dossier: ResearchDossier, report: dict[str, Any]) -> None:
        """Mechanically apply the audit. The writer only ever sees the result."""
        unsupported = {
            _key(item.get("claim", "")) for item in report.get("unsupported", []) or []
            if isinstance(item, dict)
        }
        corrections = {
            _key(item.get("claim", "")): clean_text(str(item.get("correction", "")))
            for item in report.get("corrections", []) or []
            if isinstance(item, dict) and item.get("correction")
        }
        downgrades = {
            _key(item.get("claim", "")): str(item.get("to", "")).lower()
            for item in report.get("downgrade_confidence", []) or []
            if isinstance(item, dict)
        }
        single_source = {_key(text) for text in report.get("single_source_claims", []) or []}

        kept = []
        for claim in dossier.claims:
            key = _key(claim.text)
            if key in unsupported:
                continue
            if key in corrections:
                claim.note = (f"{claim.note} " if claim.note else "") + \
                             f"Corrected during fact check: {corrections[key]}"
                claim.text = corrections[key]
            target = downgrades.get(key)
            if target in VALID_CONFIDENCE and _WEAKER[target] < _WEAKER[claim.confidence]:
                claim.confidence = target  # type: ignore[assignment]
            if key in single_source and claim.confidence == "established":
                claim.confidence = "well_evidenced"
            kept.append(claim)

        removed = len(dossier.claims) - len(kept)
        dossier.claims = kept
        if removed:
            logger.info("claims dropped as unsupported", count=removed, stage=Stage.FACTCHECK)

        for contradiction in report.get("internal_contradictions", []) or []:
            text = clean_text(str(contradiction))
            if text:
                dossier.content_notes.append(f"Internal contradiction flagged: {text}")
        if notes := clean_text(str(report.get("notes", ""))):
            dossier.summary = f"{dossier.summary}\n\nFact-check notes: {notes}".strip()


def _key(text: Any) -> str:
    return clean_text(str(text)).lower()[:180]


__all__ = ["FactChecker"]
