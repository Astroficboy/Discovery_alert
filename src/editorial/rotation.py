"""Editorial variety, without a rigid rota.

The brief is explicit that quality beats category balancing, so rotation is
implemented as a **bonus, not a filter**: a domain that has not appeared for a
while adds a few points to a candidate's score, and that is all. A genuinely
extraordinary third history story in a row still wins.

Three pressures are applied:

* *domain recency* - the longer since a domain ran, the larger the bonus;
* *music share* - music gets a nudge when it is below its configured target,
  because it is a first-class domain that a purely visual scorer would
  otherwise under-select;
* *global balance* - within music, non-Anglo-American material gets a nudge
  when recent music editions have skewed to the usual two countries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..config import Config
from ..models import Candidate, Edition


@dataclass
class RotationState:
    """A snapshot of what has run recently, and how long ago."""

    days_since_domain: dict[str, int]
    music_share: float
    dominant_music_share: float
    total_recent: int

    @classmethod
    def from_history(cls, config: Config, editions: list[Edition],
                     today: date | None = None) -> RotationState:
        today = today or date.today()
        window = config.content.avoid_recent_days
        recent = [e for e in editions if (today - e.edition_date).days <= window]

        days_since: dict[str, int] = {}
        for edition in editions:
            age = (today - edition.edition_date).days
            for domain in edition.categories or [edition.category]:
                days_since[domain] = min(days_since.get(domain, 10_000), age)

        music_editions = [e for e in recent if "music" in (e.categories or [e.category])]
        music_share = len(music_editions) / len(recent) if recent else 0.0

        dominant = set(config.music.global_balance.dominant_regions)
        dominant_count = sum(
            1 for e in music_editions
            if e.music and dominant & set(e.music.country)
        )
        dominant_share = dominant_count / len(music_editions) if music_editions else 0.0

        return cls(
            days_since_domain=days_since,
            music_share=music_share,
            dominant_music_share=dominant_share,
            total_recent=len(recent),
        )


def rotation_bonus(candidate: Candidate, state: RotationState, config: Config) -> float:
    """Points to add to a candidate's overall score, capped by config."""
    cap = config.scoring.rotation_bonus_max
    if cap <= 0:
        return 0.0
    window = max(config.content.avoid_recent_days, 1)

    domains = candidate.categories or ["uncategorised"]
    # Freshness of the *freshest* domain: a story tagged both "space" (ran
    # yesterday) and "music" (has not run in months) is still a change of pace.
    ages = [state.days_since_domain.get(domain, window * 3) for domain in domains]
    best_age = max(ages) if ages else window
    freshness = min(best_age / (window * 2), 1.0)
    bonus = cap * 0.7 * freshness

    if candidate.is_music:
        target = config.music.target_share
        if state.music_share < target:
            shortfall = (target - state.music_share) / max(target, 1e-6)
            bonus += cap * 0.2 * min(shortfall, 1.0)
        balance = config.music.global_balance
        if state.dominant_music_share > balance.max_dominant_share:
            countries = set(candidate.music.country) if candidate.music else set()
            if countries and not (countries & set(balance.dominant_regions)):
                bonus += cap * 0.2

    return round(min(bonus, cap), 2)


def cross_domain_bonus(candidate: Candidate, config: Config) -> float:
    """Reward genuine intersections. Two domains is normal; four is the sort
    of story the whole newsletter exists for."""
    settings = config.scoring.cross_domain_bonus
    extra = max(len(set(candidate.categories)) - 1, 0)
    return round(min(extra * settings.per_extra_domain, settings.max), 2)


__all__ = ["RotationState", "cross_domain_bonus", "rotation_bonus"]
